# Source de données Name Suggestion Index — documentation d'implémentation

Spécification technique à suivre pour intégrer le
[name-suggestion-index](https://github.com/osmlab/name-suggestion-index) (NSI)
comme seconde source de données.

Les chiffres cités sont mesurés sur la version Name Suggestion Index `8.0.20260729` et sur la base
de développement. Ils servent de recette : un écart significatif signale un bug.

---

## 1. Où Name Suggestion Index s'insère

Le processus de l'outil comporte quatre étapes :

1. **Acquisition** — télécharger des sources multiples, les agréger, construire
   les index.
2. **Complétion** — enchaîner les appariements entre les POI OSM et ces données
   pour construire automatiquement une version complétée des POI.
3. **Validation** — faire relire un échantillon du résultat par un humain.
4. **Publication** — pousser le jeu de données complété en masse.

**Name Suggestion Index ne touche que les étapes 1 et 2.** Il n'introduit ni nouveau flux de
revue, ni nouveau type de lot, ni nouveau chemin d'upload. Une seule contrainte
retombe sur l'étape 3, au §6.

Ce que Name Suggestion Index apporte, dans l'ordre où ça se produit :

- il **complète l'identité** des objets OSM — un `brand:wikidata` là où il n'y
  avait qu'un libellé — ce qui rend appariables des objets qui ne l'étaient pas ;
- il **complète les tags** des objets, une fois leur marque connue, avec ce que
  la source ATP ne fournit pas : la classification et les identifiants
  d'opérateur.

Name Suggestion Index décrit des **enseignes**, jamais des POI. ATP décrit des **POI** :
coordonnées, horaires, site, téléphone, courriel. Les deux sources ne se
recouvrent presque pas — leurs seules clés communes sont `brand` et `name`.

---

## 2. Étape 1 — acquisition

### 2.1 Obtenir le fichier

Un seul fichier, `dist/nsi.json`, publié dans le paquet npm
`name-suggestion-index` et servi par jsDelivr sans clé ni quota :

```
https://registry.npmjs.org/name-suggestion-index                                 # métadonnées, ~qq ko
https://cdn.jsdelivr.net/npm/name-suggestion-index@<version>/dist/json/nsi.json  # 13,5 Mo
```

Contraintes :

- `dist/` n'est plus commité sur `main` — raw.githubusercontent renvoie 404.
  npm et jsDelivr sont le seul canal.
- **Toujours épingler la version, jamais `@latest`.** Ce n'est pas qu'une
  question de cohérence avec la ligne `data_imports` : jsDelivr répond aux URL
  non épinglées depuis un cache qui peut avoir des années de retard. Il sert
  encore aujourd'hui, sous `@latest/dist/nsi.json`, un fichier de l'ère 6.x —
  un chemin que le paquet ne publie plus. Une URL épinglée est résolue contre
  le vrai tarball : un chemin erroné échoue en 404 au lieu de renvoyer
  silencieusement des données obsolètes.
- Le chemin est `/dist/json/nsi.json`. `/dist/nsi.json` était celui des
  versions 6.x.
- `_meta.version` décrit fidèlement le fichier téléchargé — mais si ce fichier
  vient du cache `@latest`, il décrit la version périmée. La référence reste
  `dist-tags.latest` du registre npm.

Rythme de publication irrégulier, par salves — de zéro à quatre releases par
mois :

```
7.0.20260126 · 7.0.20260414 · 7.1.20260427 · 7.1.20260428 · 7.1.20260501
7.1.20260507 · 7.2.20260515 · 7.2.20260530 · 8.0.20260728 · 8.0.20260729
```

Un contrôle quotidien dans le pipeline de 04:00 suffit donc largement, et ne
coûte qu'un GET sur le registre : les 13 Mo ne sont téléchargés que lorsque la
version change.

### 2.2 Structure du fichier

```json
{
  "_meta": { … },
  "nsi": {
    "brands/office/government": {
      "properties": { "path": "brands/office/government", "exclude": { … } },
      "items": [ … ],
      "templates": []
    },
    …
  }
}
```

394 catégories, dont le chemin a **toujours** la forme `<arbre>/<clé>/<valeur>`.
Quatre arbres : `brands` (278), `operators` (100), `transit` (15), `flags` (1).

Un item :

```json
{
  "displayName": "France Travail",
  "id": "francetravail-f941ae",
  "locationSet": { "include": ["fr"] },
  "matchNames": ["pôle emploi"],
  "matchTags": ["office/employment_agency"],
  "tags": {
    "brand": "France Travail",
    "brand:wikidata": "Q8901192",
    "government": "employment_agency",
    "name": "France Travail",
    "office": "government"
  }
}
```

| champ | occurrences | à en faire |
|---|---|---|
| `tags` | 48 483 | **la seule source de tags** |
| `locationSet` | 48 483 | filtre géographique (§2.3) |
| `displayName`, `id` | 48 483 | affichage Name Suggestion Index, ignorés |
| `matchNames` | 11 435 | alias pour *reconnaître* — **jamais écrit** |
| `matchTags` | 598 | classification alternative acceptée en entrée |
| `preserveTags` | 378 | regex des tags de l'objet à ne pas écraser |
| `fromTemplate` | 11 932 | item généré automatiquement, sans incidence |
| `note`, `issues` | 1 529 / 128 | commentaires mainteneurs |

**L'item ne porte pas sa clé principale** : elle est dans le chemin de sa
catégorie. `brands/office/government` → `office=government`. Le
`government=employment_agency` des tags est un tag secondaire ; la répétition du
mot est une coïncidence du vocabulaire OSM.

C'est fiable, pas heuristique : sur les arbres `brands` et `operators`,
`tags[clé]` vaut la valeur du path pour **100 %** des items (48 303 / 48 483 tous
arbres confondus ; les 180 écarts sont dans `transit`, où
`public_transport/station_subway` porte `station=subway`).

### 2.3 Construction de l'index — `select_items(nsi_json)`

Fonction pure, sans I/O, dans `src/pipeline/nsi.py`. Prend le JSON désérialisé,
retourne les lignes à insérer. C'est le cœur testable de la feature.

**a. Arbres retenus** — `brands` et `operators` seulement. `transit` et `flags`
décrivent des lignes, des réseaux et des drapeaux, que `generic.lua` exclut déjà
côté OSM via `is_definitely_not_a_place`.

**b. Filtre France** sur `locationSet`, dans l'ordre :

Codes considérés comme français : `fr` et `fx` (France métropolitaine, employé
par un millier d'items), les DROM (`gp`, `mq`, `gf`, `re`, `yt`) et les COM
(`pm`, `bl`, `mf`, `nc`, `pf`, `wf`, `tf`) — le pipeline télécharge ces régions
depuis Geofabrik, donc leurs objets atteignent `mv_places`.

1. rejeter si l'un de ces codes figure dans `exclude` ;
2. accepter si l'un de ces codes figure dans `include` ;
3. accepter si `include` contient `001`, `150`, `europe` ou `eu` ;
4. rejeter sinon.

Les entrées `*.geojson` (`fr-idf.geojson`) sont traitées par préfixe. Pas besoin
de location-conflation : les `locationSet` sont à 95 % des codes ISO.

Le filtre reste par marque et non par objet : une enseigne scopée `fx` peut en
théorie s'appliquer à un objet réunionnais. Les départager demanderait une
évaluation géographique par POI, pour une poignée de marques qui ne se
recouvrent pas.

Ce filtre n'est pas optionnel. Les 11 items McDonald's ne se distinguent pas par
leur catégorie — huit sont `amenity=fast_food` — mais par leur `locationSet` :

```
{"include":["001"], "exclude":[…,"fr",…]}  → McDonald's
{"include":["fr"]}                          → McDonald's   ← le bon
{"include":["jp"]}                          → マクドナルド
{"include":["kr"]}                          → 맥도날드
```

Sans lui, une chance sur huit de poser un nom japonais sur un McDo lyonnais.

**c. Clé principale** — lue dans le path : `path.split("/")` donne
`(arbre, primary_key, primary_value)`. Aucune liste de priorités à maintenir,
aucun cas particulier.

**d. Exiger un `brand:wikidata`** — c'est la clé de jointure de tout le système.

**e. Déduplication — l'étape critique.** Un `brand:wikidata` n'est pas une clé
unique : 2 692 QID portent plusieurs items, et ce sont les plus grosses marques.

```
Q1273376  shop=supermarket    E.Leclerc / E.Leclerc Express / Centre Commercial E.Leclerc
Q1273376  shop=jewelry        Manège à Bijoux
Q1273376  amenity=pharmacy    E.Leclerc Parapharmacie
Q1273376  amenity=fuel        Station Service E.Leclerc
```

Les items qui se distinguent par leur catégorie ne posent aucun problème : le
tag principal de l'objet OSM les départage. Ceux qui partagent une catégorie ne
gênent que s'ils **divergent sur ce qu'ils écriraient**. Les trois E.Leclerc
`shop=supermarket` ne diffèrent que par leur nom, un tag que la liste blanche
n'écrit pas : il n'y a rien à départager, et les écarter priverait la marque de
sa catégorie principale.

La règle porte donc sur les tags écrits : le groupe (QID, catégorie) est conservé
entier quand ils concordent, écarté entier sinon. **2 groupes sur 2 213** sont
écartés — Intermarché, dont le Drive porte `drive_through=only` alors que les
autres non, et un café dans le même cas.

Les libellés divergents sont conservés : ils ne sont jamais écrits, mais tous
servent à retrouver le QID depuis un nom (§3.1).

**f. Liste blanche** des tags écrits (§5) :

```python
tags = {k: v for k, v in item["tags"].items() if k in NSI_WRITABLE_TAGS}
```

**g. Sortie** — une ligne par item retenu :

```
(brand_wikidata, brand, name, primary_key, primary_value, tags)
```

`brand` et `name` sont conservés **hors** de `tags` : ce sont les libellés
d'indexation de l'étape 2, ils ne sont jamais écrits dans OSM.

Résultat attendu : **2 239 lignes** pour 1 964 QID et 2 213 couples
(QID, catégorie), dont 939 portent autre chose que le seul `brand:wikidata`.

### 2.4 Ce qui est délibérément écarté

- **L'intersection des tags entre items d'un même QID** : testée, elle ne
  conserve jamais de tag de classification et laisse 44 860 POI sans rien.
- **Exclure le QID entier dès qu'il a plusieurs items** : coûterait 61 % du
  corpus (Crédit Agricole, FedEx, Renault, Dacia, TotalEnergies…) pour une
  ambiguïté qui n'existe pas — `amenity=atm` n'est pas `amenity=bank`.

### 2.5 Schéma

Migration `019_create_nsi_brands.sql` :

```sql
CREATE TABLE nsi_brands (
    brand_wikidata  text  NOT NULL,
    brand           text  NOT NULL,
    name            text,
    primary_key     text  NOT NULL,   -- shop, amenity, office…
    primary_value   text  NOT NULL,
    tags            jsonb NOT NULL    -- sous-ensemble de NSI_WRITABLE_TAGS
);

CREATE INDEX nsi_brands_qid_idx ON nsi_brands (brand_wikidata);

CREATE INDEX nsi_brands_brand_idx ON nsi_brands (lower(brand), primary_key, primary_value);
CREATE INDEX nsi_brands_name_idx  ON nsi_brands (lower(name),  primary_key, primary_value);
```

Pas de clé primaire sur (`brand_wikidata`, `primary_key`, `primary_value`) :
plusieurs lignes la partagent volontairement, puisqu'elles se distinguent par
leurs libellés. Ce qui ne doit jamais différer, ce sont les tags qu'elles
écriraient, et c'est `select_items` qui le garantit.

### 2.6 Module et câblage

`src/pipeline/nsi.py`, trois fonctions calquées sur `atp.py` :

**`download_nsi()`**

1. `GET https://registry.npmjs.org/name-suggestion-index`, lire
   `dist-tags.latest`.
2. Comparer à la version enregistrée sur la dernière ligne `data_imports` de
   type `nsi`. Identique → retour anticipé, la branche no-ope pour le reste du
   pipeline (même mécanique que `select_run` pour ATP).
3. Sinon `start_import(conn, "nsi")` — ouvre la ligne `data_imports` de la
   branche — puis télécharger la version épinglée.

**`select_items(nsi_json)`** — §2.3.

**`import_nsi()`** — `TRUNCATE` + `COPY` en une transaction (2 239 lignes), puis
`record_import(conn, "nsi", …)` avec la version npm en commentaire.

Pas de DuckDB, pas de parquet, pas de découpage : le fichier tient en mémoire et
le volume final est négligeable.

Câblage dans `src/pipeline/dag.py` :

```python
"start":        (None, ["osm-download", "atp-download", "nsi-download"]),
"nsi-download": (download_nsi, ["nsi-import"], {"lock": "network"}),
"nsi-import":   (import_nsi, ["osm-views"]),
```

`nsi-import` doit précéder `osm-views` : `setup_mv_places` consomme
`nsi_brands` (§3.1).

**À ne pas oublier** : `record_failure` dérive `import_type` du préfixe du nom
d'étape et retombe sur `"pipeline"` hors `osm`/`atp`. Ajouter `nsi` à sa liste,
sinon un échec se journalise en `pipeline` et la ligne ouverte par
`start_import(… "nsi")` reste béante.

---

## 3. Étape 2 — complétion

### 3.1 Complétion de l'identité, dans `mv_places`

`mv_places` est déjà une projection normalisée d'OSM. Sa colonne
`brand_wikidata` devient une valeur **complétée** plutôt que copiée :

```sql
COALESCE(
    tags->>'brand:wikidata',
    (SELECT n.brand_wikidata FROM nsi_brands n
      WHERE (n.primary_key, n.primary_value) = (<clé principale de l'objet>)
        AND (lower(n.brand) = lower(tags->>'brand')
             OR lower(n.name) = lower(tags->>'name')))
)
```

Ajouter une colonne `brand_wikidata_source` valant `'osm'` ou `'nsi'` : elle est
indispensable en aval (§3.3 et §6).

La **double condition** — libellé *et* catégorie — est ce qui rend l'opération
sûre. `renault` seul est ambigu ; `renault` + `shop=car_repair` ne l'est pas.
Vérifié sur la totalité du jeu : les 2 209 entrées d'index ne mènent jamais à
deux QID différents.

Rendement mesuré :

| population `mv_places` | objets | QID retrouvés | ambigus |
|---|---|---|---|
| `brand` en texte, pas de QID | 13 623 | **2 227** | 0 |
| `name` seul, ni `brand` ni QID | 883 646 | **5 009** | 0 |
| **total** | | **7 236** | **0** |

```
241 objets → Q6686    Renault          184 → Q3356080
 88 objets → Q590952  Crédit Agricole  172 → Q24189171
 80 objets → Q6742    Peugeot          114 → Q112064766
```

750 de ces objets portent une marque qu'`atp_fr` connaît déjà : ils deviennent
appariables par QID au tour suivant et rejoignent le flux ATP normal — horaires,
site, téléphone, courriel.

**Sur les objets écartés en amont** : `generic.lua` élimine ceux qui n'ont ni
nom, ni marque, ni courriel, ni téléphone, ni site — environ 95 % du PBF. Name Suggestion Index ne
peut rien pour eux : il s'indexe sur un libellé, ils n'en ont aucun. Le filtre
reste correct et ne doit pas être relâché. Les 883 646 objets « à nom seul » ne
sont pas dans ce cas : ils étaient conservés mais invisibles à tout appariement
par QID, et c'est là que la complétion travaille.

### 3.2 Effet sur l'appariement ATP

Aucun changement dans `MATCHED_POI_SQL`. Sa clause
`osm.brand_wikidata = atp.brand_wikidata` opère désormais sur la valeur
complétée : les objets rattrapés au §3.1 entrent d'eux-mêmes dans le périmètre.

`mv_places_brand` se recalcule sur la même base et voit donc les nouvelles
marques apparaître dans les lots. Rien à modifier non plus.

### 3.3 Complétion des tags, dans le diff

Dans `apply_on_node` (`src/matching.py`), après le calcul des tags ATP.

Sélection de l'entrée `nsi_brands`, avec `qid` le `brand:wikidata` retenu par le
diff :

```
lignes = nsi_brands WHERE brand_wikidata = qid

  0 ligne   → ne rien faire
  1 ligne   → elle s'applique ; si l'objet porte une clé principale
              d'une autre catégorie, la clé principale de l'entrée est
              retirée avant écriture (Name Suggestion Index ne reclasse pas)
  N lignes  → soit p = (clé principale de l'objet OSM, sa valeur)
              p est None            → ne rien faire
              une ligne matche p    → elle s'applique
              aucune ligne ne matche → ne rien faire
```

La condition sur la clé principale n'est pas un prérequis du match : c'est un
désambiguïsateur, il ne sert que lorsqu'il y a une ambiguïté. C'est ce qui permet
de traiter les objets dépourvus de tag principal, dont le QID n'a qu'une seule
catégorie.

Elle reste malgré tout un garde-fou de classification quand elle ne désambiguïse
rien : un objet `shop=supermarket` portant `brand:wikidata=Q89029184` (les
stations Casino, seule entrée Name Suggestion Index de ce QID) recevait `amenity=fuel` par-dessus.

Exemple, sur l'entrée `brands/office/government` de France Travail — une seule
ligne pour `Q8901192` :

```
entrée Name Suggestion Index   {"office": "government",
              "government": "employment_agency",
              "brand:wikidata": "Q8901192"}

objet A      office=government, name=France Travail
             → clé principale identique, l'entrée s'applique entière
             → +government=employment_agency, +brand:wikidata

objet B      office=employment_agency, name=France Travail
             → clé principale d'une autre catégorie : `office` est retiré
             → +government=employment_agency, +brand:wikidata
             → office=employment_agency reste ce que le contributeur a mis

objet C      name=France Travail, sans clé principale
             → rien à contredire, l'entrée s'applique entière
```

Seule la classification est en cause : ce qui la porte est retiré, tout le reste
passe. Le désaccord est le plus souvent une nuance de sous-type
(`shop=telecommunication` côté Name Suggestion Index, `shop=mobile_phone` côté OSM) — et comme
`shop` n'est pas dans `NSI_WRITABLE_TAGS`, il n'y a alors rien à retirer.

Lecture de la clé principale **de l'objet OSM** (il n'a pas de path), par ordre
de priorité fixe :

Implémenté par `osm_primary_tag` (migration 019), en SQL et non en Python : la
fonction est appelée depuis la construction de `mv_places`. `unnest` préserve
l'ordre du tableau, donc `LIMIT 1` retient la première clé présente sur l'objet
et renvoie `[clé, valeur]`, ou `NULL`.

```sql
SELECT ARRAY[k, osm_tags->>k]
FROM unnest(ARRAY['shop', 'amenity', 'tourism', 'office', 'leisure',
                  'healthcare', 'craft', 'landuse']) AS k
WHERE osm_tags ? k
LIMIT 1;
```

Cette liste ne sert qu'à *lire* l'objet, jamais à interpréter Name Suggestion Index.

Elle ne retient que les clés qui peuvent réellement atteindre `mv_places` :
`man_made`, `advertising`, `highway` et `waterway` en sont absentes parce que
`generic.lua` écarte ces objets à l'import, bien avant.

Deux conséquences de l'ordre, arbitraire et propre à l'outil — OSM n'a pas de
notion de tag principal :

- une station-service avec boutique (`amenity=fuel` + `shop=convenience`) est
  lue comme un `shop`, donc en désaccord avec une entrée Name Suggestion Index `amenity=fuel` ;
- une valeur multiple (`shop=convenience;gas`) n'égale aucune catégorie Name Suggestion Index,
  l'objet compte donc toujours comme en désaccord.

Dans les deux cas on s'abstient, ce qui est la direction sûre.

Écriture : pour chaque clé de `tags` absente de l'objet OSM, la poser. Plus le
`brand:wikidata` lui-même quand il vient de Name Suggestion Index (`brand_wikidata_source = 'nsi'`)
— c'est le tag principal produit par toute la chaîne.

Deux règles absolues :

- **jamais d'écrasement** — c'est ce qui rend `brand:wikidata` sûr à 100 %, et ce
  qui empêcherait, si la liste s'élargissait un jour, de renommer 1 529 agences
  SG en Société Générale ;
- **une catégorie OSM qui ne correspond à aucune entrée du QID ne produit
  rien**. Name Suggestion Index ne corrige pas une classification, il complète. Pas d'arbitrage
  entre ce que dit la marque et ce qu'un contributeur a relevé sur le terrain.

### 3.4 Rendement total de l'étape 2

| | objets | tags |
|---|---|---|
| `brand:wikidata` complétés (§3.1) | 7 236 | 7 236 |
| tags complétés sur objets appariés (§3.3) | 1 049 | 1 057 |
| tags complétés sur les objets rattrapés | 7 236 | 472 |

L'essentiel de la valeur est le `brand:wikidata` : c'est la clé de jointure de
tout l'outil — `MATCHED_POI_SQL`, `mv_places_brand`, le découpage en lots. Les
tags secondaires sont un bonus, pas la justification.

---

## 4. Étapes 3 et 4 — inchangées

Name Suggestion Index n'introduit ni flux de revue, ni type de lot, ni chemin d'upload nouveau.
Les objets rattrapés entrent dans `mv_places_brand` comme les autres et suivent
`pack_departements`, `select_batch`, le cooldown et `BulkUpload` sans
modification.

**Une seule contrainte retombe sur l'étape 3** : quand `brand_wikidata_source`
vaut `'nsi'`, le QID est *déduit d'un libellé*, pas lu sur l'objet. L'UI de
revue doit le montrer. Sans ça, le relecteur valide une inférence en croyant
relire une donnée — et c'est l'inférence la plus forte que fait la chaîne.

---

## 5. Périmètre des tags écrits

**Seuls ces dix tags sont écrits dans OSM.** Toute autre clé de Name Suggestion Index est jetée à
l'import (§2.3.f). C'est le seul endroit qui matérialise ce choix, et donc le
seul à modifier pour l'élargir.

```python
NSI_WRITABLE_TAGS = frozenset(
    {
        "brand:wikidata",
        "amenity",
        "tourism",
        "office",
        "leisure",
        "operator:wikidata",
        "official_name",
        "government",
        "drive_through",
        "healthcare:speciality",
    }
)
```

La liste est issue d'une mesure d'accord entre Name Suggestion Index et le terrain, sur les
109 577 objets OSM appariés à une entrée Name Suggestion Index unique. Tous affichent **99,4 %
d'accord ou plus** :

| tag | accord | n |
|---|---|---|
| `brand:wikidata` | 100 % | 109 577 |
| `amenity` | 100 % | 31 405 |
| `tourism` | 100 % | 2 923 |
| `operator:wikidata` | 99,9 % | 4 756 |
| `leisure` | 99,9 % | 1 348 |
| `government` | 99,8 % | 666 |
| `official_name` | 99,7 % | 1 839 |
| `drive_through` | 99,7 % | 666 |
| `healthcare:speciality` | 99,7 % | 317 |
| `office` | 99,4 % | 8 681 |

Exclus, avec la raison :

| tag | accord | écarts typiques |
|---|---|---|
| `name` | 86,8 % | `TotalEnergies`/`Total` (589), `Société Générale`/`SG` (408) |
| `operator` | 87,2 % | 586 fois `Société Générale` contre `SG` |
| `brand` | 96,8 % | 1 529 `SG`, 639 `Total`, 187 `Total Access` |
| `shop` | 98,2 % | 1 157 écarts, souvent de vraies reclassifications locales |
| `cuisine` | 98,2 % | `french_tacos`/`mexican` (36) |
| `takeaway` | 98,4 % | 79 objets en `only` là où Name Suggestion Index dit `yes` |
| `clothes` | 98,3 % | `underwear`/`lingerie` |
| `vending` | 14,3 % | `drinks;sweets` ne correspond presque jamais |

Deux conséquences :

- `operator:wikidata` est écrit **sans** `operator`. Un objet portant
  `operator=SG` reçoit `operator:wikidata=Q270363` : le QID est juste quel que
  soit le libellé local, et c'est justement ce que le libellé ne donne pas.
- `shop` n'étant pas dans la liste, la clé principale n'est écrite que lorsqu'elle
  vaut `amenity`, `tourism`, `office` ou `leisure`. Elle reste lue et utilisée
  comme discriminant dans tous les cas.

---

## 6. Tests

`tests/test_nsi.py`, portant sur `select_items` uniquement — mini-JSON en dur,
pas de fixture, pas de réseau. Quatre cas, un par règle :

| cas | vérifie |
|---|---|
| McDonald's, ses 11 items | seul celui de `include:["fr"]` survit (§2.3.b) |
| E.Leclerc, ses 15 items | tout passe sauf les trois `shop=supermarket` qui s'annulent (§2.3.e) |
| Crédit Agricole | deux lignes (`bank`, `atm`), `operator:wikidata` retenu et `operator` jeté (§2.3.f) |
| un item `exclude:["fr"]` | rejeté (§2.3.b) |

La complétion du §3.1 étant du SQL dans `mv_places`, elle se vérifie par la
recette plutôt que par un test unitaire.

---

## 7. Recette

Après le premier import complet, sur la base de développement :

| contrôle | attendu |
|---|---|
| `SELECT count(*) FROM nsi_brands` | 2 239 |
| `SELECT count(DISTINCT brand_wikidata) FROM nsi_brands` | 1 964 |
| lignes avec plus que `brand:wikidata` dans `tags` | 939 |
| `mv_places WHERE brand_wikidata_source = 'nsi'` | 7 236 |
| … dont issus d'un `brand` | 2 227 |
| … dont issus d'un `name` seul | 5 009 |
| tags secondaires ajoutés sur les objets déjà appariés | 1 057 |

Un écart significatif signale un bug dans le filtre France (§2.3.b), dans la
dédup (§2.3.e) ou dans la liste blanche (§2.3.f).

Contrôle qualitatif à faire une fois : vérifier qu'aucune ligne de `nsi_brands`
ne porte de caractères non latins dans `brand` — ce serait le signe que le filtre
France laisse passer des variantes linguistiques.

---

## 8. Extensions

Chacune se résume à ajouter une clé à `NSI_WRITABLE_TAGS`. Ni migration, ni
changement de la logique de sélection.

1. Les attributs de marque — `cuisine`, `takeaway`, `clothes`, `beauty`,
   `self_service` (98 à 99,5 % d'accord), en lot de revue séparé.
2. `shop` — 98,2 %, mais c'est une clé principale : l'ajouter permettrait de
   classer les objets qui n'ont aucune catégorie.
3. Les variantes de nom — `short_name`, `alt_name`, `brand:en`, `brand:fr`
   (100 % d'accord, sur 57 à 416 objets seulement).
4. `matchNames` comme troisième libellé d'indexation au §3.1 : +99 objets
   mesurés, mais 3 cas ambigus apparaissent — à n'ouvrir qu'avec une règle de
   rejet des ambiguïtés.
5. `name` (86,8 %) et `operator` (87,2 %) — en dernier, si jamais.

## 9. Hors périmètre, définitivement

- **location-conflation** — une dépendance JS entière pour affiner un filtre qui
  traite déjà 95 % des `locationSet` en dix lignes.
- **Relâcher `is_definitely_not_a_place`** — les objets sans aucun libellé ne
  sont pas rattrapables par Name Suggestion Index, qui s'indexe précisément sur un libellé.
- **Les 2 groupes (QID, catégorie) aux tags contradictoires** — Intermarché et
  un café : rien ne permet de choisir entre `drive_through=only` et son absence.
- **Les 27 marques ATP absentes de Name Suggestion Index** (Mercedes-Benz Vans, Sushi Daily,
  Delko, MAN…) — la longue traîne, 1 379 POI. Rien à faire ici, sinon contribuer
  en amont à Name Suggestion Index.
