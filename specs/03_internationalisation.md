# Sortir de France

Spécification technique — à relire avant implémentation.

L'outil est aujourd'hui monopays. L'objectif est qu'un second pays coûte un
fichier de configuration, et que le passage du deuxième au troisième ne coûte
rien de plus. Le présent document décrit ce qui bloque, dans quel ordre le
lever, et ce qu'on choisit explicitement de ne pas faire.

Une non-cible : **une instance ne sert qu'un pays.** Pas de multi-pays dans une
même base, pas de sélecteur de pays dans l'interface. Un déploiement = un pays =
une base. La langue, elle, peut être multiple : un pays en a une ou plusieurs
(§4.1), et l'interface les propose toutes. Un seul pays par instance, c'est ce
qui rend la chose faisable ; le jour où deux pays doivent cohabiter, la
conversation recommence.

Les chiffres cités sont ceux mesurés sur la base française de développement. Ils
servent de recette : un écart significatif signale un bug.

---

## 1. État des lieux

Le cœur est déjà agnostique et ne bouge pas : `MATCHED_POI_SQL` (hors rayon),
`apply_on_node`, `mv_places`, `pack_departements`, `select_batch`, les
cooldowns, `BulkUpload`, l'OAuth OSM, le runner de pipeline, `generic.lua`
(aucune trace de la France).

Cinq dépendances à lever, par coût décroissant :

| # | Dépendance | Où | Nature |
|---|---|---|---|
| 1 | Le département comme unité de lot | `atp.py`, `matching.py`, schéma | Structurelle |
| 2 | La langue de l'interface et des changesets | `website/templates/`, `upload.py` | Volumineuse, mécanique |
| 3 | La calibration NSI | `nsi.py` | Cachée, échoue en silence |
| 4 | `normalize_phone` | migration 012 | Bug, indépendamment de l'i18n |
| 5 | Les constantes pays | `constants.py`, `atp.py`, `nsi.py` | Config |

---

## 2. Phase A — `normalize_phone` (préalable, indépendant)

**À faire en premier, seul, sans rapport avec le reste.** C'est une correction
de bug qui se trouve aussi débloquer l'international.

### Le problème

```sql
REGEXP_REPLACE($1, '^\+\d{1,3}', '0')   -- +33 → 0
```

La fonction ramène un numéro international à une forme nationale à préfixe
« 0 ». Cette convention est française (et allemande, et britannique). Elle est
fausse en Espagne et aux États-Unis (pas de préfixe d'acheminement), et en
Italie où le 0 initial fait partie du numéro. Hors de France, deux numéros
distincts peuvent se normaliser vers la même chaîne : le match est alors un
faux positif silencieux, des deux côtés de la jointure.

La fonction est `IMMUTABLE` et sert dans deux index fonctionnels
(`atp_fr_phone_norm_idx`, `mv_places_phone_norm_idx`). `CREATE OR REPLACE` ne
les touche pas : leurs entrées restent celles calculées par l'ancienne
définition, et le planificateur les lit comme si elles correspondaient. La
migration les reconstruit (`REINDEX`).

### La cible

Ne pas normaliser vers une forme nationale, et ne pas non plus se contenter des
derniers chiffres. La clé est le **numéro national significatif** : les chiffres
qui restent une fois retirés, dans cet ordre, le préfixe international puis le
préfixe d'acheminement.

```sql
-- 00 33 1 23 45 67 89  →  123456789
-- +33 1 23 45 67 89    →  123456789
-- +33 (0)1 23 45 67 89 →  123456789
-- 01 23 45 67 89       →  123456789
```

**Les deux préfixes se retirent l'un après l'autre, jamais comme des
alternatives** : `+33 (0)1 23 45 67 89` porte les deux, et c'est une écriture
dont OSM est plein.

Le raccourci qui semblait suffisant — garder les neuf derniers chiffres — est
**faux hors de France**, et c'est le genre de faux qui ne se voit pas. Il ne
marche que parce qu'un numéro français a toujours neuf chiffres significatifs :
la forme nationale (`0` + 9) et la forme internationale (`33` + 9) se terminent
alors sur les mêmes neuf. En Allemagne, où le numéro significatif fait de six à
onze chiffres, `030 123456` donne `030123456` et `+49 30 123456` donne
`930123456` : deux clés différentes pour un même numéro. Une fenêtre de longueur
fixe ne peut pas convenir à des numéros de longueur variable.

Deux valeurs seulement dépendent du pays — les indicatifs et le préfixe
d'acheminement :

```sql
WITH country AS (
  SELECT ARRAY['33','262','508','590','594','596','681','687','689']
           AS calling_codes,
         '0' AS trunk_prefix
)
```

**Une liste d'indicatifs, pas un seul** : la France répond à neuf. La Réunion et
Mayotte sont en +262, la Guadeloupe en +590, la Guyane en +594, la Martinique en
+596, Wallis en +681, la Nouvelle-Calédonie en +687, la Polynésie en +689,
Saint-Pierre en +508. OSM contient les deux écritures de ces numéros ; n'en
lister qu'un ferait que l'internationale et la nationale ne se rencontrent
jamais outre-mer. Le plus long indicatif qui correspond l'emporte.

Ce sont des **constantes de la fonction, pas des paramètres** : elle est
`IMMUTABLE` et indexée, donc elle ne peut rien lire au moment de l'appel. Une
instance ne servant qu'un pays, c'est sans conséquence.

**Et c'est pourquoi la fonction n'est pas définie par une migration.** Une
migration est du code livré : y écrire l'indicatif ferait payer une modification
du dépôt à chaque nouveau pays, alors que toute la spécification tient sur
l'inverse — un pays coûte un fichier de configuration. La fonction est donc
**engendrée** (`src/phone.py`), installée au démarrage de l'application et en
tête de pipeline, et réinstallée quand les deux valeurs bougent. C'est le même
principe que `mv_places`, engendrée par `_mv_places_sql()`.

L'installation porte sa propre garde : la signature de la définition est
estampée sur la fonction en `COMMENT`, comparée avant tout travail, et un
changement déclenche le `REINDEX` des index construits dessus. Sans lui, un
index garde les clés calculées par la définition précédente et un parcours
d'index contredit silencieusement un parcours séquentiel.

Jusqu'à la phase D, les deux valeurs sont des constantes de `src/phone.py` ;
ensuite elles viennent du fichier pays, et rien d'autre ne change.

Le retrait de l'indicatif nu est gardé par une longueur restante d'au moins six
chiffres, pour qu'un numéro court commençant par l'indicatif reste entier
(`3310` n'est pas un `+33 10`).

### Mesuré sur une base clonée de la production

219 175 POI ATP, 289 271 objets OSM porteurs d'un téléphone.

| Mesure | Ancienne | Nouvelle |
|---|---|---|
| Paires appariées par le téléphone, à 500 m | 35 929 | **38 915** |
| Paires gagnées | — | 2 987 |
| Paires perdues | — | **1** |
| Valeurs refusées (`NULL`) côté ATP | 0 | 98 (0,10 %) |
| Valeurs refusées côté OSM | 0 | 1 906 (0,66 %) |

**+8,3 %, et c'est un gain, pas une dérive.** Le seuil de 1 % que ce document
annonçait supposait l'ancienne fonction correcte ; elle ne l'était pas. Son
`REGEXP_REPLACE(phone, '^\+\d{1,3}', '0')` est glouton : sur `+33229402873`,
écrit sans espaces, il mange `+332` et laisse `029402873`, là où
`+33 2 29 40 28 73` donne `0229402873`. Les 2 987 paires gagnées sont
massivement ce cas — des correspondances que l'outil ratait.

**La seule paire perdue est un faux positif supprimé** : un `+33 5 46 28 06 00`
français apparié à un `+31 546 280 600` néerlandais situé à moins de 500 m.
L'ancienne fonction retirait n'importe quel indicatif ; la nouvelle ne retire que
ceux du pays, donc le numéro néerlandais garde ses chiffres et ne rencontre plus
personne.

Les valeurs refusées ne coûtent rien : elles n'apparaissent dans aucune paire
perdue. Ce sont des annotations et des listes — `01.44.41.55.79 - 01 44 41 55 82`,
`01.55.91.10.10 standard`, `01 45 87 42 43 ou 01 45 87 41 08`.

**La recette pour la suite** n'est donc pas un seuil mais une exigence :
**chaque paire gagnée ou perdue doit s'expliquer.** Les chiffres ci-dessus sont
la référence à retrouver.

### Un cas à trancher avant d'écrire la fonction

Trois formes que la fonction actuelle **mange en silence**, et que la nouvelle
mangerait aussi si on n'y touche pas :

- **les valeurs multiples.** OSM autorise `phone=01 23 45 67 89;01 23 45 67 88`.
  En ne gardant que les chiffres, on obtient une chaîne de 20 chiffres dont les 9
  derniers sont… le second numéro. Deux commerces qui partagent leur second
  numéro s'apparient alors sur un numéro que ni l'un ni l'autre n'affiche en
  premier ;
- **les extensions.** `+33 1 23 45 67 89 poste 12` ajoute deux chiffres au
  numéro significatif et produit une clé qui ne correspond à rien ;
- **le texte libre.** `sur rendez-vous`, `0800-GO-OSM` : il en reste toujours
  quelques chiffres, donc toujours une clé.

Décision : **une valeur contenant une lettre, un séparateur de liste (`;`, `,`
ou `/`) ou plus de 15 chiffres ne se normalise pas — la fonction rend `NULL`.**
La lettre est ce qui attrape les extensions et le texte libre d'un seul coup ;
le seul préfixe alphabétique légitime, `tel:`, est retiré avant le test. `NULL` n'est jamais
égal à rien : la ligne cesse simplement d'être appariable par le téléphone,
tandis que les autres critères (marque, nom, site, courriel) continuent de jouer.
Perdre un appariement est sans gravité ; en fabriquer un faux écrit une bêtise
dans OpenStreetMap.

Quinze chiffres, parce que c'est le maximum d'un numéro E.164 : au-delà, la
valeur n'est pas un numéro.

### Livrable

`src/phone.py` — le générateur `normalize_phone_sql(calling_code,
trunk_prefix)` et l'installateur `ensure_normalize_phone(conn, …)` — câblé au
démarrage de l'application et en tête de pipeline. Aucune migration. Et
**une batterie de tests exhaustive** — c'est une fonction de dix lignes qui
décide de ce qui part dans OpenStreetMap, elle doit être couverte comme telle.

`tests/test_normalize_phone.py`, tables de cas paramétrées par `pytest.mark.
parametrize`, exécutées contre un vrai Postgres (le comportement testé est celui
de la fonction SQL, pas d'une réimplémentation Python) :

**1. Équivalence — toutes ces écritures d'un même numéro doivent produire la même
clé.** Une classe d'équivalence par numéro, et l'assertion porte sur la classe
entière, pas sur des paires choisies :

```
+33 1 23 45 67 89   0033 1 23 45 67 89   01 23 45 67 89   01.23.45.67.89
01-23-45-67-89      (01) 23 45 67 89     +33 (0)1 23 45 67 89
0123456789          01 23 45 67 89       tel:+33-1-23-45-67-89
```

Y compris les espaces que produisent les vrais éditeurs : espace insécable
(U+00A0), espace fine insécable (U+202F), tabulation, espaces en tête et en
queue.

**2. Discrimination — ces valeurs doivent produire des clés différentes.**
Numéros voisins d'un chiffre, en tête comme en queue ; numéros de longueurs
différentes ; le même numéro à des indicatifs régionaux différents.

**3. Formes courtes.** Les numéros à 3, 4 et 6 chiffres (`3949`, `118 712`,
services clients) : `RIGHT(…, 9)` rend la chaîne entière, ce qui est correct,
mais il faut vérifier qu'un numéro court ne collisionne pas avec la fin d'un
numéro long. C'est le seul endroit où la fenêtre de neuf chiffres peut mordre,
et il mérite ses propres cas.

**4. Rejets.** Les cas de la section précédente — valeurs multiples, extensions,
plus de quinze chiffres — plus la chaîne vide, les espaces seuls, une valeur sans
aucun chiffre (`sur rendez-vous`), une valeur en lettres (`0800-GO-OSM`).
Attendu : `NULL`, jamais une clé partielle.

**5. `NULL` et `STRICT`.** `normalize_phone(NULL) IS NULL`, et la fonction reste
`IMMUTABLE STRICT PARALLEL SAFE` — l'assertion porte sur le catalogue
(`pg_proc.provolatile`, `proparallel`, `proisstrict`), parce qu'un attribut perdu
casse les index fonctionnels sans casser aucun test de comportement.

**6. Non-régression sur la fonction remplacée.** Pour un corpus de numéros
français bien formés, l'ancienne et la nouvelle fonction doivent **partitionner à
l'identique** : mêmes groupes d'équivalence. C'est la garantie que la France ne
bouge pas. Les cas où elles divergent sont listés explicitement dans le test,
avec la raison — ce sont les seuls changements admis.

**7. Sur données réelles, avant déploiement.** Sur la base de développement
clonée de la production :

- nombre de paires appariées **par le seul téléphone** avant / après — écart
  attendu inférieur à 1 %, et chaque paire apparue ou disparue est inspectée à la
  main si elle dépasse la dizaine ;
- nombre de valeurs distinctes qui s'effondrent sur la même clé, des deux côtés
  (`atp_places`, `mv_places`) : il ne doit pas augmenter ;
- nombre de valeurs devenues `NULL` : c'est la mesure du point 4 sur le terrain,
  attendue faible et entièrement constituée de listes et d'extensions. On la
  regarde, on ne la subit pas.

Ces trois mesures sont un script, `scripts/check_phone_normalization.py`, pas un
mode d'emploi : il installe l'ancienne fonction sous un autre nom dans un schéma
jetable, compare, et signale lui-même les seuils dépassés.

**Et la batterie se vérifie elle-même.** Une suite qui passe ne prouve rien tant
qu'on n'a pas cassé la fonction pour la voir échouer. Six mutations, à rejouer
après toute modification : préfixe d'acheminement non retiré, listes acceptées,
garde de longueur de l'indicatif supprimée, `PARALLEL SAFE` retiré, `REINDEX`
supprimé, garde de signature ignorée. Chacune doit faire échouer au moins un
test.

---

## 3. Phase B — La subdivision administrative

C'est le chantier. Tout le reste en dépend, y compris les libellés à traduire.

### Le problème

Le département est partout l'unité de découpage : lot, changeset OSM, cooldown,
blocage, affichage, export CSV. Il est obtenu de deux manières, toutes deux
françaises :

1. **Dérivé du code postal** dans `src/pipeline/atp.py` — deux premiers
   chiffres, trois si le code commence par 97 ou 98, plus un filtre
   `REGEXP '^(2[AB]|[0-9]{2})[0-9]{3}$'` qui rejette les POI dont le code postal
   n'est pas français.
2. **Nommé par un dict codé en dur** — `DEPARTEMENT_NAMES` dans
   `src/matching.py`, 101 départements et collectivités, avec son cas Corse
   documenté en commentaire.

Le point 1 ne se généralise pas par configuration. Le code postal encode une
subdivision administrative en France, en Allemagne partiellement, et nulle part
ailleurs : au Royaume-Uni, aux Pays-Bas, en Irlande, en Pologne, aux États-Unis,
il n'y a aucune fonction du code postal vers un découpage utilisable. Un
paramètre du type « nombre de caractères significatifs » serait une
généralisation fausse.

### La cible : rattachement spatial

Les frontières administratives sont **déjà dans le PBF qu'on télécharge**.
`generic.lua` les jette aujourd'hui (`is_definitely_not_a_place` rejette
`boundary` et `admin_level`, et `has_no_matchable_tag` finirait le travail). On
les récupère dans une table dédiée, et on rattache chaque POI ATP par
`ST_Contains`.

La configuration par pays se réduit alors à **un entier** : le niveau le plus
fin souhaité, `admin_level = 6` en France. À partir de là, on décroît
automatiquement — 6, 5, 4, 3, 2 — et on retient le premier polygone qui contient
le POI. Pas de liste à énumérer : le découpage administratif d'un pays n'est pas
homogène (voir §4.0), et personne n'a envie de deviner par avance quels niveaux
intermédiaires existent.

Le niveau 2 est le pays lui-même : **tout POI du pays tombe dans au moins un
polygone**. La règle « aucun rattachement ⇒ rejet » ne rejette donc plus qu'une
seule chose, et c'est exactement ce qu'on veut : un POI dont les coordonnées
tombent hors du pays.

Les noms viennent d'OSM (`name`), plus de dictionnaire à maintenir.

#### B0 — Ce que `admin_level = 6` couvre réellement en France

Vérifié sur Overpass (août 2026). Le niveau 6 ne suffit pas :

| Territoire | Niveau disponible | `ref:INSEE` |
|---|---|---|
| Départements métropolitains | **6** | `01`…`95`, `2A`, `2B` |
| Paris | **6** | `75` |
| Rhône / Métropole de Lyon | **6** (deux polygones) | `69D`, `69M` |
| Guadeloupe, La Réunion, Mayotte | **6** | `971`, `974`, `976` |
| Martinique, Guyane | **4** (pas de niveau 6) | `972R`, `973R` |
| Nouvelle-Calédonie | 3, provinces en **4** | `988` |
| Polynésie française | **3** seulement | `987` |
| Wallis-et-Futuna | **3** | `986` |
| Saint-Pierre-et-Miquelon, Saint-Barthélemy, Saint-Martin | **3** | `975`, `977`, `978` |

Martinique et Guyane sont des collectivités territoriales uniques : elles n'ont
pas de relation `admin_level=6`. Les COM du Pacifique non plus. Or
Nouvelle-Calédonie, Polynésie et Wallis-et-Futuna **sont téléchargées depuis
Geofabrik** et portent des POI appariables aujourd'hui : un filtre `= 6` les
ferait disparaître silencieusement, puisque la règle « aucun rattachement ⇒
rejet » les rejetterait toutes.

D'où la décroissance depuis `admin_level = 6`. En France métropolitaine le
niveau 6 gagne toujours ; en Guadeloupe aussi (elle a les deux) ; en Martinique
et Guyane le 4 prend le relais ; en Nouvelle-Calédonie ce sont les trois
provinces (niveau 4), granularité de changeset plus fine que le territoire
entier, ce qui est un progrès ; Polynésie et Wallis tombent au niveau 3.

Le repli est borné par la taille des lots, pas par le niveau atteint : une
subdivision plus grosse qu'un lot forme son propre lot et `select_batch` la
tronque, comportement déjà en place et déjà testé. Un pays sans aucune
subdivision exploitable resterait donc utilisable, un lot après l'autre.

Saint-Pierre-et-Miquelon, Saint-Barthélemy, Saint-Martin et les TAAF n'ont
**aucun extrait Geofabrik téléchargé** — aucun objet OSM ne peut s'y apparier.
Aujourd'hui le code postal leur attribue quand même un numéro (975, 977, 978,
984) qui ne sert à rien. Le rattachement spatial les fait disparaître d'eux-mêmes,
sans cas particulier : c'est une correction, pas une régression.

#### B0 bis — Les codes changent, l'historique ne se réécrit pas

`subdivision_code` vaut `COALESCE(ref:INSEE, ref, osm_id)`. Pour la France, cinq
familles de codes diffèrent de ceux dérivés du code postal aujourd'hui :

| Aujourd'hui | Demain |
|---|---|
| `20` (Corse) | `2A` et `2B` |
| `69` | `69D` et `69M` |
| `972`, `973` | `972R`, `973R` |
| `986`, `987` | inchangés (`ref:INSEE` porte bien `986`, `987`) |
| `988` | trois provinces néo-calédoniennes |

**On ne traduit pas l'historique.** Une table de correspondance appliquée à
toutes les lignes serait une réécriture du passé fondée sur une heuristique :
les cas un-vers-plusieurs (`20`, `69`, `988`) n'ont pas de réponse exacte au
niveau de la ligne, et dupliquer une ligne sur deux codes fausse les totaux
affichés. Une ligne d'historique enregistre **ce qui s'est passé** : le
changeset a bien été créé pour « la Corse », et c'était vrai.

Deux changements suffisent, et aucun n'est une heuristique.

**1. L'historique devient autoportant.** `import_subdivisions` gagne une colonne
`subdivision_name TEXT NOT NULL`, remplie à l'insertion. L'affichage de
l'historique ne joint plus rien : il lit le code et le nom tels qu'ils étaient au
moment de l'intégration. Le backfill de cette colonne pour les lignes existantes
est exact — c'est l'ancien `DEPARTEMENT_NAMES`, appliqué une fois, sur des codes
qui ne changent pas. C'est aussi ce qui rend l'historique immunisé contre un
futur redécoupage administratif, français ou non.

**2. Les lignes encore sous cooldown ne sont pas retraitées.** C'est le seul
endroit où un ancien code a un effet sur le présent : `get_blocked_subdivisions`
compare des codes d'historique à des codes de `mv_places_brand`. Un `69` ne
rencontre plus ni `69D` ni `69M`, donc son cooldown cesse de bloquer.

**Et c'est sans conséquence, parce que le cooldown est une ceinture par-dessus
des bretelles.** Ce qui empêche vraiment de reproposer un POI déjà intégré,
c'est le rafraîchissement quotidien : il relit OSM, y trouve les tags qu'on
vient d'écrire, et le POI sort des correspondances. Le cooldown ne couvre que
la fenêtre entre l'envoi et le rafraîchissement suivant. Le perdre sur les
lignes concernées — 158 au 28 août 2026, sur 5 818 sous cooldown, toutes en
`69`, `20`, `972` ou `973` — expose au plus un lot vide ou presque.

Une résolution par la donnée était possible : `osm_changeset_id` donne l'emprise
du changeset via l'API OSM, et l'emprise tombe dans une subdivision. Elle a été
écrite, puis jetée. Cent cinquante lignes, un appel réseau, une exécution
manuelle à ne pas oublier le jour du déploiement, et un cas — l'emprise à cheval
sur deux subdivisions nouvelles — qui n'a pas de réponse au niveau de la ligne :
c'est cher payé pour un blocage que la donnée fraîche assure déjà. Passé le
cooldown, un ancien code ne sert plus qu'à l'affichage, et l'affichage est réglé
par le point 1.

#### B0 ter — Recette de la migration d'historique

**Exigence : correspondance à 100 %, vérifiée sur les données de production, pas
sur un jeu de test.** L'API d'export publique fournit exactement ce qu'il faut,
sans accès à la base :

```
curl -s https://atp2osm.fr/api/export/history.json      > history.json
curl -s https://atp2osm.fr/api/export/departements.json > departements.json
```

Procédure, à rejouer sur une base locale chargée de ces deux fichiers :

1. compter les lignes avant migration, par statut et par code ;
2. exécuter la migration ;
3. **assertions bloquantes**, toutes obligatoires :
   - même nombre de lignes `import_subdivisions` qu'avant — aucune création,
     aucune suppression ;
   - `subdivision_name` non nul partout, et identique à ce que
     `DEPARTEMENT_NAMES` donnait pour le code correspondant ;
   - somme des `items_count` inchangée, globalement et par ligne d'historique
     parente ;
   - aucune ligne d'`import_history` orpheline ou dupliquée ;
   - aucun `subdivision_code` n'est modifié : la migration ne touche que le
     nom, et les lignes sous cooldown restent bit-à-bit identiques à avant.
4. rejouer la migration une seconde fois sur la base déjà migrée : elle doit être
   idempotente et ne rien changer (le test existe déjà pour le backfill 016,
   `test_migration_backfill.py`).

Un seul écart bloque le déploiement. Ce n'est pas une recette indicative : la
migration ne part pas tant que les assertions ne passent pas sur l'export de
production du jour. Elles sont couvertes par
`tests/test_migration_subdivisions.py`.

#### B1 — Extraire les frontières

Dans `generic.lua`, une troisième table et un chemin de traitement distinct,
**avant** les filtres existants (qui restent inchangés pour les POI) :

```lua
tables.subdivisions = osm2pgsql.define_area_table('subdivisions', {
    { column = 'osm_id',      type = 'text', not_null = true },
    { column = 'ref',         type = 'text' },
    { column = 'name',        type = 'text', not_null = true },
    { column = 'admin_level', type = 'int',  not_null = true },
    { column = 'geom',        type = 'geometry', projection = srid, not_null = true },
})
```

Alimentée dans `process_relation` par les relations
`type=boundary` + `boundary=administrative`, de `admin_level` 2 jusqu'à
`admin_level_max` inclus — on filtre au SQL, pas au Lua, pour pouvoir changer de
niveau sans réimporter 20 Go de PBF.

`admin_level_max` est une clé de la configuration pays (§5.1), pas une constante.
La valeur 8 (la commune en France) convient à un pays qui découpe par
départements, mais rien ne dit qu'elle convienne partout : certains pays placent
leur échelon utile en 9, 10 ou 11, et Paris, Lyon et Marseille ont leurs
arrondissements municipaux en 10. Plafonner en dur interdirait de les configurer
un jour sans réimporter tout le PBF, ce qui est précisément ce que ce plafond
sert à éviter.

Il doit valoir au moins `admin_level` — le chargeur le vérifie — et il vaut mieux
le prendre franchement plus haut : sa seule fonction est de laisser de la marge.
Le surcoût est faible et connu à l'avance : quelques milliers de polygones
jusqu'au niveau 6, environ 35 000 de plus au niveau 8 en France, l'ordre de
grandeur des communes du pays.

Le Lua ne lit pas la configuration : `run_osm2pgsql` passe la valeur en variable
d'environnement, que `generic.lua` récupère par
`tonumber(os.getenv("ATP2OSM_ADMIN_LEVEL_MAX")) or 8`.

`ref` est conservé parce qu'il porte le code officiel (`ref:INSEE` en France, et
`ref` tout court dans la plupart des pays) — c'est lui qui devient
l'identifiant stable de la subdivision, avec repli sur `osm_id` quand il manque.

#### B2 — Renommer le concept

Migration de renommage, mécanique et sans perte :

- table `import_departements` → `import_subdivisions`
- colonne `departement_number` → `subdivision_code` (partout : `atp_*`,
  `mv_places_brand`, `import_*`, index)
- `pack_departements` → `pack_subdivisions`, `count_by_departement` →
  `count_by_subdivision`, `get_blocked_departements` →
  `get_blocked_subdivisions`
- suppression de `DEPARTEMENT_NAMES`, remplacé par une jointure sur
  `subdivisions`

Les libellés affichés restent français dans cette phase — la traduction est la
phase C. Le renommage touche aussi les tests (`test_pack_departements.py`,
`test_backfill_departements.py`, `test_migration_backfill.py`) et l'endpoint
`/api/export/departements.csv`, qui devient
`/api/export/subdivisions.csv` **avec conservation de l'ancienne URL en
redirection permanente** : elle est documentée publiquement dans `docs.html`.

#### B3 — Rattacher les POI

Dans `atp.py`, remplacer la dérivation postale :

```sql
-- avant : SUBSTRING(postcode, 1, 2)
-- après :
LEFT JOIN LATERAL (
    SELECT s.code, s.name
    FROM subdivisions s
    WHERE s.admin_level <= :admin_level
      AND ST_Contains(s.geom, ST_GeomFromGeoJSON(atp.geom))
    ORDER BY s.admin_level DESC
    LIMIT 1
) s ON TRUE
```

Le filtre `addr:country = 'FR'` reste (phase D), mais **le filtre par regex de
code postal disparaît**. Il servait de contrôle qualité — un POI dont le code
postal n'a pas la forme française est probablement mal géocodé. Il est remplacé
par un contrôle strictement meilleur : **un POI qui ne tombe dans aucune
subdivision est rejeté.** Ça attrape en plus les POI dont le code postal est
correct mais dont les coordonnées sont fausses, ce que la regex laissait passer.

Ordre des opérations dans le pipeline : `subdivisions` est produite par
`osm2pgsql`, donc l'étape OSM doit précéder l'import ATP. C'est déjà le cas dans
le DAG, mais la dépendance devient réelle et doit être exprimée dans
`src/pipeline/dag.py`.

#### B4 — Recette

À exécuter sur la base française avant de déclarer la phase terminée :

- le nombre de POI dans `atp_places` ne varie pas de plus de 1 % (les écarts
  attendus sont des POI mal géocodés désormais rejetés, et des POI sans code
  postal désormais acceptés) ;
- pour 99 % au moins des POI conservés, la subdivision rattachée spatialement
  correspond au département dérivé du code postal ;
- **les désaccords sont examinés un par un**, pas agrégés : ce sont soit des POI
  en limite de département, soit des bugs. Un désaccord de plus de 1 % n'est pas
  acceptable et bloque la phase ;
- le total par marque affiché dans la liste ne change pas ;
- **chaque subdivision peuplée aujourd'hui a exactement un successeur**, COM
  comprises : la Nouvelle-Calédonie, la Polynésie et Wallis-et-Futuna comptent
  toujours des POI après bascule. Zéro POI dans l'un des trois = le repli par
  niveau ne fonctionne pas, la phase est bloquée ;
- les lignes d'`import_subdivisions` antérieures conservent leur code, y compris
  ceux que `subdivisions` ne produit plus : elles enregistrent ce qui s'est
  passé, pas ce qui se produirait aujourd'hui.

#### B5 — Ce qu'on ne fait pas

- **Pas de gestion des subdivisions hétérogènes en taille.**
  `pack_subdivisions` fait déjà du bin-packing sur une clé opaque et gère le
  cas d'une subdivision plus grosse qu'un lot. Aucune raison de le retoucher.
- **Pas de découpage hiérarchique** (région → département → commune). Un seul
  niveau, celui de la config.
- **Pas de gestion des chevauchements.** Un POI dans deux subdivisions du même
  `admin_level` n'existe pas ; si le cas se produit, l'`ORDER BY` du `LATERAL`
  tranche de façon déterministe et on passe à autre chose.
- **Pas de niveau choisi par territoire.** `admin_level` est global au pays ; on
  ne configure pas « niveau 4 en Guyane, 6 ailleurs ». La décroissance y pourvoit
  sans qu'aucun territoire soit nommé.
- **Pas de plancher configurable.** La décroissance va jusqu'au niveau 2 et
  s'arrête là ; seul le plafond d'import (`admin_level_max`) se règle. Un pays
  qui voudrait exclure ses territoires lointains le fait en
  ne les téléchargeant pas depuis Geofabrik, pas en réglant un niveau.

---

## 4. Phase C — Internationalisation de la langue

Volumineuse (~1 840 lignes de gabarits), sans risque technique. À faire **après**
la phase B, sinon on traduit deux fois les libellés de découpage.

### C1 — L'interface

Flask-Babel, extraction depuis les gabarits. **Un pays peut avoir plusieurs
langues** : la Suisse en a quatre, la Belgique trois. `Country.locales` est donc
un tuple ordonné dont le premier élément est la langue par défaut, celle sur
laquelle tout retombe.

Une instance sert toujours **un seul pays** (§1) ; ce qu'on ouvre ici, c'est le
choix de la langue à l'intérieur de ce pays, pas le choix du pays.

#### La langue est dans le chemin

`/de/brands`, jamais `?lang=de`. Le sous-répertoire est la forme que les moteurs
segmentent proprement ; le paramètre d'URL est celle que la documentation Google
déconseille pour les variantes de langue — consolidation agressive des
paramètres, langue invisible dans l'URL partagée, et une seconde URL créée dès
qu'un tri ou un filtre est présent.

**Chaque page traduite est préfixée, y compris dans la langue par défaut.** Une
URL sans préfixe ne sert rien : elle négocie et redirige (302) vers
`/<locale>/…`. C'est elle que porte le `x-default`, et c'est le seul endroit où
le cookie et `Accept-Language` interviennent encore :

1. le cookie `lang`, posé à chaque page servie dans une langue ;
2. `Accept-Language`, restreint aux locales du pays ;
3. `country.locales[0]`.

Un code de langue inconnu (`/es/brands`) est retiré, puis la même négociation
s'applique. Une fois sur une URL préfixée, **le chemin fait autorité** : ni le
cookie ni le navigateur ne peuvent servir une autre langue que celle affichée.

**Le mécanisme.** Un middleware WSGI déplace le préfixe de `PATH_INFO` vers
`SCRIPT_NAME`. Werkzeug préfixe alors tous les `url_for` tout seul : aucune
route, aucun `url_for`, aucun `nav_item` n'a à connaître la langue. Le seul
ajout dans les gabarits est `request.script_root` devant les `href` écrits en
dur.

Ne sont **pas** préfixés — ils ne portent pas de langue et le middleware les
laisse passer : les assets, `robots.txt`, `sitemap.xml`, `llms.txt`, le rappel
OAuth et tout ce qui n'est pas une page. La liste positive des chemins traduits
vit dans `app.py`, à côté des routes : une nouvelle route est sans langue par
défaut, donc rien ne casse en silence. Un asset référencé depuis une page passe
par `static_url()`, qui échappe au préfixe — sinon `SCRIPT_NAME` dupliquerait le
cache des assets par langue.

`sitemap.xml` liste les pages publiques croisées avec les locales du pays, chaque
entrée portant ses `xhtml:link` alternates et un `x-default` sur l'URL sans
préfixe. `llms.txt` pointe la langue courante. Pour la France, une seule locale.

**Ce que ça n'est pas.** Pas de préférence stockée par compte OSM : le cookie
suffit, et rien côté serveur ne survit à un redéploiement aujourd'hui (le jeton
lui-même vit dans le cookie signé).

#### Le sélecteur

Affiché **seulement si `len(country.locales) > 1`**. Une instance française n'a
aucun sélecteur à l'écran, et le gabarit n'a pas de cas particulier à porter :
la condition est la même partout.

**Où.** Dans `_aside.html`, juste au-dessus du bloc « À propos », au même niveau
que le bloc utilisateur en bas de la barre latérale. Pas dans `_base.html` : le
bandeau du haut n'existe qu'en affichage mobile et ne porte que le logo.

**Quoi.** Des liens, pas un `<select>` :

```jinja
{% if country.locales | length > 1 %}
<nav class="w-full shrink-0" aria-label="{{ _('Langue') }}">
  <details class="dropdown dropdown-top w-full">
    <summary class="btn btn-ghost btn-sm w-full justify-start">
      <i class="iconoir-language"></i>
      <span class="sidebar-hide">{{ locale_name(get_locale()) }}</span>
    </summary>
    <ul class="menu dropdown-content bg-base-100 rounded-md shadow-sm w-full">
      {% for code in country.locales %}
      <li><a href="{{ lang_url(code) }}" hreflang="{{ code }}"
             {% if code == get_locale() %}aria-current="true"{% endif %}>
        {{ locale_name(code) }}</a></li>
      {% endfor %}
    </ul>
  </details>
</nav>
{% endif %}
```

Trois conséquences de ce choix, toutes voulues :

- **aucun JavaScript.** `<details>` fait l'ouverture, les entrées sont de vrais
  `<a href>`. Le sélecteur fonctionne sans JS, comme le reste du site — la seule
  page qui en dépend aujourd'hui est l'authentification (`auth.js`) ;
- **chaque langue est une URL réelle**, donc crawlable : c'est le même mécanisme
  qui alimente les `hreflang` et le `sitemap.xml` ci-dessus. Un `<select>` avec
  un `onchange` aurait cassé les deux ;
- **la barre latérale repliée est déjà gérée** : `sidebar-hide` sur le libellé
  laisse l'icône seule, exactement comme le bouton de connexion. Rien à ajouter
  dans `sidebar.js` ni dans `app.css`.

`locale_name` rend le nom de la langue **dans cette langue** (« Deutsch », pas
« Allemand ») : c'est ce qu'attend quelqu'un qui cherche à sortir d'une langue
qu'il ne lit pas. `babel.Locale.parse(code).get_display_name(code)` le fournit,
aucune table à tenir.

**`lang_url(code)`** est un global Jinja qui reconstruit l'URL courante en
remplaçant le préfixe de langue — chemin, filtres, tri et pagination
préservés. Changer de langue ne doit jamais renvoyer à l'accueil ni perdre un
tri en cours (`history.html` et `todo.html` en ont).

**Le cookie.** Posé dans un `after_request` à chaque page servie dans une
langue : nom `lang`, un an, `SameSite=Lax`, **hors de la session signée**. Il ne
décide de rien sur une URL préfixée ; il ne sert qu'à la négociation d'une URL
sans préfixe. Le choix de langue n'a rien à voir avec l'authentification : le
mettre dans la session le ferait disparaître à la déconnexion et l'interdirait
aux visiteurs non connectés, qui sont précisément le public de `/` et `/docs`.

**Ce qu'il reste à toucher dans `_base.html`** — quatre valeurs codées en dur
qui ne sont pas des chaînes traduisibles et que l'extraction Babel ne verra
donc pas :

- `<html lang="fr">` → `{{ get_locale() }}` ;
- `<meta property="og:locale" content="fr_FR">` → la locale courante, plus un
  `og:locale:alternate` par autre langue du pays ;
- `<link rel="canonical">` → `request.base_url`, qui porte déjà le préfixe :
  chaque langue est canonique sur sa propre URL ;
- un `<link rel="alternate" hreflang="…">` par locale, plus `x-default` vers
  l'URL sans préfixe, celle qui négocie.

**Ce qu'on ne fait pas.** Pas de préférence de langue stockée par compte OSM :
le cookie suffit, et rien côté serveur ne survit à un redéploiement aujourd'hui
(le jeton lui-même vit dans le cookie signé). Pas de sélection de langue par sous-domaine
ni par ccTLD : le sous-répertoire donne la même séparation sans multiplier les
certificats ni les déploiements.

Concerne : `website/templates/` (14 gabarits + `brands/`, `errors/`),
`src/error_reasons.py`, et le contenu éditorial — `docs.html`, les `meta` et
`og:description` de `home.html`, `llms.txt`, `sitemap.xml`.

**Les dates.** `locale.setlocale(LC_TIME, "fr_FR.utf8")` disparaît : c'est un
état global au processus, non thread-safe sous gunicorn, et il ne peut porter
qu'une langue — l'inverse de ce qu'un pays multilingue demande. Les
`strftime('%d/%m/%Y')` codés en dur dans les gabarits passent à `format_date` /
`format_datetime`, qui lisent la locale active de la requête.

`docs.html` est de la documentation rédigée, pas des libellés : elle se traduit
comme un texte, éventuellement en la sortant des gabarits vers du Markdown rendu.

### C2 — Le commentaire de changeset

`src/upload.py:80` écrit dans OpenStreetMap :

```python
"comment": f"Intégration des données ATP ({dept_label}; {self.brand_name})"
```

C'est du **contenu public déposé dans OSM**, pas de l'interface. Il doit suivre
une langue du pays cible — un contributeur anglophone intégrant des données
allemandes écrit un commentaire allemand, parce que c'est la communauté
allemande qui le relira.

Il devient donc un gabarit porté par la configuration pays (§5), pas une chaîne
traduite — **un par locale du pays** :

```python
changeset_comments = {
    "de": "ATP-Datenimport ({subdivision}; {brand})",
    "fr": "Intégration des données ATP ({subdivision} ; {brand})",
    "it": "Importazione dati ATP ({subdivision}; {brand})",
}
```

Dans un pays multilingue, le commentaire suit **la langue choisie par le
contributeur**, avec repli sur `locales[0]`. Un contributeur qui travaille en
français à Genève écrit un commentaire français : c'est bien une langue du pays,
donc lisible par la communauté qui relira. La règle inchangée est qu'aucune
langue étrangère au pays ne peut atterrir dans un changeset — une interface en
anglais sur une instance française produit toujours un commentaire français.

Le raffinement qu'on ne fait pas : suivre la langue **de la subdivision**
(le canton, la commune) plutôt que celle du contributeur. Ce serait plus juste au
Tessin, et ça demanderait une carte linguistique par subdivision. Le jour où une
communauté le réclame, `Country` saura porter un dict par code de subdivision.

### C3 — L'anglais dans le code

`AGENTS.md` impose l'anglais dans le code ; la règle a fui par endroits :
`src/matching.py:299` (le commentaire sur la Corse — il disparaît avec la phase
B), `src/upload.py:156`, `src/pipeline/atp2osm.py:11`, la migration 015, et
plusieurs commentaires SQL dans `MATCHED_POI_SQL`. À nettoyer dans cette phase :
c'est le moment où le projet devient lisible par des non-francophones.

Les messages de commit restent en français (règle inchangée).

---

## 5. Phase D — Les constantes de configuration

La partie facile, et la seule qui corresponde vraiment à « quelques
configurations stables dans le temps ».

### D1 — Un fichier JSON par pays

**Un fichier de données, pas du code, et fourni de l'extérieur.** Une instance
n'est pas toujours servie par quelqu'un qui édite le dépôt : si le produit se
distribue en image conteneur, l'opérateur allemand fournit sa configuration
**sans toucher au code installé**. Un module Python l'obligerait à patcher
l'intérieur du paquet ou à maintenir un fork ; un fichier se monte dans le
conteneur.

**Aucun fichier pays n'est livré avec le produit** — pas même celui de la
France. Le dépôt ne contient aucun `countries/*.json`, et l'instance française
monte le sien exactement comme les autres. C'est la seule façon de garantir que
le chemin de configuration externe est réellement testé : un fichier par défaut
embarqué serait le chemin que tout le monde emprunte, et celui des tiers
pourrirait sans que personne s'en aperçoive.

```json
{
  "code": "de",
  "geofabrik": ["europe/germany"],
  "admin_level": 6,
  "admin_level_max": 8,
  "locales": ["de"],
  "changeset_comments": {
    "de": "ATP-Datenimport ({subdivision}; {brand})"
  },
  "nsi_locations": ["de", "150", "eu", "001"],
  "nsi_writable_tags": ["brand:wikidata"],
  "nsi_calibration": {
    "nsi_version": null,
    "measured_at": null,
    "agreement": {}
  },
  "match_radius_m": 500,
  "timezone": "Europe/Berlin"
}
```

Une seule façon de le désigner, une variable d'environnement, **sans valeur par
défaut** :

```
COUNTRY_CONFIG=/etc/atp2osm/country.json
```

Absente ou illisible, l'application refuse de démarrer — `ConfigError`, comme
`OSM_DB_NAME` aujourd'hui. Pas de repli silencieux sur la France.

Où vit ce fichier, concrètement :

- **en production**, à côté de `.env`, monté en volume dans le conteneur par le
  quadlet. Il n'est pas dans le dépôt du produit ; il appartient au déploiement,
  au même titre que les secrets OAuth ;
- **en développement**, `dev.sh` le lie symboliquement depuis le checkout
  principal au premier `up`, exactement comme il le fait déjà pour `.env` et
  `node_modules`. Une ligne à ajouter au script, et les worktrees héritent de la
  configuration sans copie ;
- **pour démarrer un pays**, `calibrate_nsi_tags.py --init <code>` écrit un
  squelette complet avec les valeurs par défaut et les clés vides à remplir.
  L'exemple est **généré**, pas livré : il ne peut pas diverger du chargeur.

(Si tu veux malgré tout un `country.json.sample` versionné pour la
documentation, c'est le précédent de `.env.sample` et ça ne coûte rien — mais
c'est un fichier d'exemple, jamais un défaut chargé.)

Le fichier est chargé une fois, validé, puis figé dans la même dataclass
`Country` que précédemment — le reste du code ne voit toujours qu'un objet gelé,
jamais un dict :

```python
@dataclass(frozen=True)
class Country:
    """Everything that differs from one country to the next."""

    code: str  # ISO 3166-1 alpha-2, matched against addr:country
    geofabrik: tuple[str, ...]  # extract paths, without -latest.osm.pbf
    admin_level: int  # finest subdivision level; falls back down to 2
    admin_level_max: int  # deepest level imported; >= admin_level
    locales: tuple[str, ...]  # first one is the default and the fallback
    changeset_comments: dict[str, str]  # per locale; {subdivision} and {brand}
    nsi_locations: frozenset[str]
    nsi_writable_tags: frozenset[str]
    match_radius_m: int = 500
    timezone: str = "UTC"


@lru_cache(maxsize=1)
def get_country() -> Country:
    return _parse(json.loads(_country_path().read_text()))
```

#### Ce que JSON coûte, et comment on le paie

**Pas de commentaires.** C'est la vraie perte, et elle tombe pile sur
`nsi_writable_tags`, dont chaque entrée existe pour une raison mesurée (§6.2).
On ne la compense pas par une clé `"_comment"` : on la remplace par **de la
donnée**. Le bloc `nsi_calibration` porte la version NSI, la date de mesure et
le taux d'accord relevé pour chaque tag — c'est le script de calibration qui
l'écrit, et il n'est jamais rédigé à la main :

```json
"nsi_calibration": {
  "nsi_version": "8.0.20260729",
  "measured_at": "2026-07-30",
  "agreement": { "brand:wikidata": 1.0, "amenity": 0.997, "official_name": 0.994 }
}
```

C'est mieux qu'un commentaire : ça se régénère, ça se compare d'une version NSI
à la suivante, et un tag présent dans `nsi_writable_tags` mais absent
d'`agreement` se repère automatiquement. **Le chargeur émet un avertissement au
démarrage** dans ce cas — un tag écrit sans mesure derrière lui.

**Aucune validation par le langage.** Le chargeur la fait, à la main, une
vingtaine de lignes sans dépendance : clés requises présentes, clés inconnues
refusées (une faute de frappe est une erreur, pas un silence), types conformes,
`locales` non vide, `code` en deux lettres minuscules, chaque locale de
`changeset_comments` appartenant à `locales`. Ça échoue **au démarrage**, avec le
nom de la clé fautive — pas au premier appel qui la lit, trois heures après le
début du pipeline.

Cette validation n'est pas une concession au format : c'est de toute façon ce
qu'il faut quand la configuration vient de l'extérieur du dépôt. Un module
Python l'aurait rendue superflue, mais il aurait aussi exigé que la personne qui
la fournit soit développeuse.

**Les tests** construisent un `Country` directement, ou chargent un JSON écrit
dans `tmp_path`. Aucun ne dépend d'un fichier du dépôt, ce qui est la
conséquence logique de n'en livrer aucun : **la configuration française n'est
plus une donnée de test disponible**. Les tests qui ont besoin d'un pays
plausible se donnent le leur, en trois lignes, dans une fixture.
### D2 — Ce que ça remplace

- **`constants.py`** — `_GEOFABRIK_PATHS`, dict en dur métropole + DOM + COM du
  Pacifique. Devient la liste `geofabrik`. Le cas français (neuf extraits, dont
  des territoires classés sous `australia-oceania`) reste exprimable tel quel :
  c'est une liste de chemins, pas une convention.
- **`atp.py`** — `WHERE addr:country = 'FR'` devient `country_code`. La table
  `atp_fr` est renommée `atp_places` ; le nom fuit dans `mv_places_brand` et
  `tests/test_export.py`, à corriger avec.
- **`atp.py`** — `_FOREIGN_COUNTRY_CODES`, la liste ISO moins `fr`. Devient la
  liste ISO moins `country_code`, calculée, plus codée en dur. La règle de
  lecture ne bouge pas : **seul le suffixe du nom de spider est lu comme un code
  pays, jamais un préfixe.** C'est ce qui évite de prendre `la_halle_fr` ou
  `au_vieux_campeur` pour du laotien et de l'australien, et le raisonnement vaut
  dans toutes les langues — un préfixe de deux lettres est presque toujours un
  morceau du nom de marque, un suffixe presque toujours un code pays. Rien à
  configurer, rien à vérifier par pays.

- **`matching.py`** — le rayon de 500 m de `ST_DWithin`, calibré sur une densité
  urbaine européenne. Devient `match_radius_m`. Ne pas y toucher pour
  l'Allemagne ou l'Espagne ; le rediscuter pour les États-Unis ou l'Australie.
- **`run-pipeline.sh` / le timer systemd** — 04:00 Europe/Paris. Devient le
  fuseau du pays.
- **`Containerfile`** — `locale-gen fr_FR.UTF-8`. L'image embarque en dur la
  locale système française. Devient l'ensemble des locales du pays, dérivé de
  `locales` au moment du build, ou plus simplement `locale-gen C.UTF-8` plus les
  locales voulues passées en `ARG`. À ne pas oublier : c'est ce qui détermine le
  formatage des nombres et des dates côté serveur, et l'oubli ne se voit qu'à
  l'affichage.

### D3 — Ce qu'on ne fait pas

Pas de schéma JSON, pas de Pydantic, pas de couche de migration de
configuration. Une fonction `_parse` qui lit un dict et rend un `Country`, et
qui lève sur ce qu'elle ne comprend pas. Pas de classe de base à sous-classer
par pays, pas de `CountryProfile` avec des surcharges : `Country` est un sac de
valeurs gelé. Le jour où un pays a besoin de **code** et pas de valeurs — une
règle de rattachement qui ne tient pas dans un entier — on en reparle. Pas
avant.

Pas de versionnage du format non plus. Un champ ajouté a une valeur par défaut,
un champ retiré est ignoré avec un avertissement. Le jour où une rupture est
inévitable, elle sera annoncée dans les notes de version comme le reste.

Pas de variable d'environnement par réglage non plus : `COUNTRY_CONFIG` seule
désigne le pays, tout le reste vient du fichier. Un réglage qu'on voudrait
modifier sans changer de pays est le signe qu'il n'appartient pas au pays mais
au déploiement, et il a alors sa place dans `.env` avec les autres.

### D4 — Ce qui appartient au déploiement, pas au pays

La règle ci-dessus tranche un cas concret : **le lien GitHub** du bloc « À
propos » de `_aside.html`, aujourd'hui codé en dur sur
`hagatopaxi/atp2osm-import`. Une instance allemande tenue par quelqu'un d'autre
pointe vers son propre dépôt.

Ce n'est pas une propriété du pays : deux instances issues du même fork, l'une
en France l'autre en Allemagne, partagent le même dépôt, et deux forks servant
le même pays n'ont pas le même. Ça va donc dans `.env` et dans la dataclass
`App`, pas dans `Country` :

```
SOURCE_REPO_URL=https://github.com/<owner>/atp2osm-import
```

Optionnelle, avec le dépôt amont comme valeur par défaut — une instance qui ne
la règle pas continue de pointer vers l'original, ce qui est le comportement
souhaitable pour un fork non publié.

Restent codées en dur, et c'est délibéré : la licence GPL-3.0 (c'est celle du
code, pas un réglage), l'attribution ODbL d'OpenStreetMap (imposée par la
licence des données), le nom `atp2osm` et le logo. Le jour où quelqu'un
rebaptise son instance, il touchera au gabarit — inutile de construire un
système de marque blanche pour un besoin que personne n'a exprimé.

---

## 6. Phase E — La calibration NSI

La dépendance la plus discrète, et celle qui échoue le plus mal : elle ne casse
rien, elle tague de travers.

### E1 — Le périmètre géographique

`_FRENCH` et `_WORLDWIDE` dans `src/pipeline/nsi.py` sélectionnent les items NSI
dont le `locationSet` couvre la France. Devient la liste `nsi_locations` de la
configuration : le code pays, ses territoires dépendants, et les codes
englobants (`001` monde, le continent, `eu` le cas échéant).

Le filtre reste **par marque, jamais par objet** — le raisonnement documenté
dans `_is_french` ne dépend pas du pays.

Un point de vigilance à documenter, pas à corriger tout de suite : `_is_french`
ignore les `locationSet` qui pointent un fichier `*.geojson` régional, au motif
que ça représente 5 % des cas et qu'un traitement correct imposerait
`location-conflation`, une dépendance JavaScript entière. Le raccourci tient
pour un grand pays ; dans un petit pays ou une région transfrontalière, la part
de `locationSet` régionaux monte et le raccourci devient faux. **Recette à
exécuter pour chaque nouveau pays** : compter la part d'items dont le
`locationSet` est régional. Au-dessus de 10 %, le raccourci ne tient plus et il
faut en reparler.

### E2 — `NSI_WRITABLE_TAGS` — la vraie difficulté

La liste des tags que NSI a le droit d'écrire n'est pas une constante
universelle. Le commentaire de `nsi.py` le dit : elle a été établie **en
mesurant, pour chaque tag NSI, son taux d'accord avec ce que les objets OSM
français de la même marque portent déjà**, et en ne gardant que ceux au-dessus
de 99,4 %. Des tags ont été écartés parce que leurs désaccords sont
systématiques et non du bruit — NSI dit « Société Générale » là où le terrain
dit « SG ».

Ce raisonnement est intrinsèquement national. Rien ne garantit que `official_name`
ou `operator:wikidata` se comportent en Allemagne comme en France, et un tag
mal classé ne produit aucune erreur : il produit des milliers d'objets OSM mal
tagués, validés par un humain qui n'a aucune raison de se méfier.

La liste devient donc `nsi_writable_tags` dans la configuration, **mais avec une
règle explicite : elle ne se recopie pas d'un pays à l'autre.** Elle se produit
par la même mesure, sur les données du pays.

D'où le livrable central de cette phase : **un script de calibration**,
`scripts/calibrate_nsi_tags.py`, qui prend la base d'un pays et sort le tableau
taux d'accord par tag, avec le nombre d'objets observés. Sans lui, la
configuration NSI d'un nouveau pays est un vœu pieux. Avec lui, ouvrir un pays
coûte une exécution et une lecture de tableau.

Le script écrit **directement les deux clés du fichier pays** (§5.1) :
`nsi_writable_tags`, les tags au-dessus du seuil, et `nsi_calibration`, la
mesure complète qui justifie la sélection. Personne ne recopie un taux à la
main, et une nouvelle version de NSI se réévalue en relançant le script et en
comparant les deux blocs.

Un pays sans calibration démarre avec `nsi_writable_tags = ["brand:wikidata"]`
— le seul tag dont la justesse ne dépend pas de la langue — et s'enrichit après
mesure. C'est le défaut sûr.

`_TREES`, `_UNREACHABLE_KEYS` et `_UNREACHABLE_LANDUSE` restent codés en dur :
ils dérivent de `generic.lua`, qui ne dépend d'aucun pays.

---

## 7. Ordre, découpage et coût

Chaque phase est indépendamment livrable et n'a de sens que dans cet ordre.
Une phase = une PR ; la phase B est assez grosse pour être découpée en B1..B3,
chacune un commit, dans une seule PR.

| Phase | Contenu | Bloque quoi |
|---|---|---|
| A | `normalize_phone` | Rien — correction autonome |
| B | Subdivision spatiale | Tout le reste |
| C | i18n interface + changeset | Rien |
| D | Fichier de configuration pays | Rien après B |
| E | Calibration NSI + script | Ouverture d'un pays |

**Le coût honnête.** Le premier pays supplémentaire coûte A+B+C+D+E, c'est-à-dire
l'essentiel du travail. Le deuxième coûte un fichier JSON, une exécution de
`calibrate_nsi_tags.py`, une relecture de la liste des spiders écartés et une
traduction de gabarits. C'est ça, le coût marginal visé : borné, mesurable, mais
pas nul — la calibration NSI et la vérification des spiders demandent un humain
qui connaît le pays.

Deux jalons pour vérifier qu'on tient la promesse :

1. après B+D, l'outil tourne sur la Belgique francophone **sans une ligne de
   code nouvelle** — même langue, subdivisions différentes, c'est le test
   isolant de la phase B ;
2. après C+E, il tourne sur l'Allemagne. C'est le test complet.

---

## 8. Exigences de test

Le principe vaut pour toutes les phases : **ce qui décide de ce qui part dans
OpenStreetMap se teste exhaustivement, pas par échantillon.** Une fonction courte
n'est pas une fonction sans risque — `normalize_phone` fait dix lignes et arbitre
des milliers d'appariements.

La règle pratique : pour chaque unité de décision, écrire les **classes
d'équivalence** (tout ce qui doit donner le même résultat) et les **classes de
discrimination** (tout ce qui doit donner un résultat différent), puis
paramétrer. Pas trois paires bien choisies.

**Phase A** — voir §2, sept familles de cas.

**Phase B.** `subdivision_for(point)` est la nouvelle unité de décision, et elle
remplace une dérivation triviale par une requête spatiale : elle mérite la même
couverture que la phase A.

- un point par subdivision **de chaque niveau atteint** : métropole (6), Guadeloupe
  (6), Martinique (4), Guyane (4), Nouvelle-Calédonie (4), Polynésie (3),
  Wallis (3) — la table `subdivisions` de test est petite et écrite à la main ;
- les cas limites géométriques : point exactement sur une frontière, point dans
  une enclave, point en mer dans les eaux territoriales, point hors du pays
  (attendu : rejet) ;
- la priorité de niveau : un point couvert par un 6 **et** un 4 rend le 6 ; un
  point couvert seulement par un 4 rend le 4 ; l'ordre ne dépend pas de l'ordre
  d'insertion en base ;
- `pack_subdivisions`, `select_batch`, `count_by_subdivision`,
  `get_blocked_subdivisions` : les tests existants sont **conservés tels quels**,
  renommés seulement. S'ils passent après renommage, l'algorithme n'a pas bougé —
  c'est exactement ce qu'on veut prouver ;
- la migration d'historique : les assertions de B0 ter, sur l'export de
  production du jour, plus l'idempotence.

**Phase C.** Ce qui se teste vraiment, sans tester Babel lui-même :

- le middleware de préfixe : préfixe valide (le chemin fait autorité, même
  contre un cookie contraire), préfixe inconnu (retiré puis négocié), préfixe
  absent (redirection vers cookie, puis `Accept-Language`, puis la locale par
  défaut), query string conservée, chemin sans langue laissé intact ;
- `lang_url()` : conservation du chemin, des filtres, du tri et de la pagination,
  remplacement du préfixe déjà présent, URL sans query ;
- **aucune chaîne non traduite** : un test qui charge chaque catalogue et échoue
  s'il reste des `msgstr` vides ou des `#, fuzzy` ;
- **aucune locale manquante** : pour chaque locale du pays, un
  `changeset_comments` existe et contient bien `{subdivision}` et `{brand}`.
  C'est le test qui empêche un changeset de partir avec un gabarit cassé.

**Phase D.** Le chargeur de configuration est une frontière de confiance — il lit
un fichier écrit à l'extérieur du dépôt :

- chaque clé requise, absente une par une ;
- chaque clé, avec un type faux ;
- une clé inconnue (attendu : refus, pas un avertissement) ;
- `admin_level_max < admin_level`, `locales` vide, `code` mal formé, locale de
  `changeset_comments` absente de `locales`, `geofabrik` vide ;
- fichier absent, JSON invalide, fichier vide ;
- un fichier complet et valide : tous les champs arrivent intacts dans le
  `Country`, y compris les valeurs par défaut des clés optionnelles.

**Phase E.** `select_items` est déjà couvert par `tests/test_nsi.py` ; la
paramétrisation par pays y ajoute :

- `_is_country()` sur chaque forme de `locationSet` : code du pays, code
  englobant, code d'un territoire, code étranger, exclusion, `*.geojson`, vide ;
- un item multi-catégories, un groupe qui diverge, un groupe qui converge — les
  cas existants, rejoués avec une configuration pays autre que la France, pour
  prouver qu'aucune constante française ne subsiste dans le code ;
- le script de calibration : sur un jeu de données construit, il doit retenir
  exactement les tags au-dessus du seuil et écrire un `nsi_calibration` complet.

---

## 9. Décisions explicites

- **Une instance, un pays.** Pas de multi-tenance, pas de sélecteur de pays.
- **Un pays peut avoir plusieurs langues.** `locales[0]` est la langue par
  défaut et le repli ; le sélecteur n'apparaît que s'il y en a plusieurs.
- **Un seul niveau de subdivision par POI**, choisi par décroissance depuis
  `admin_level` jusqu'au pays — jamais par territoire nommé.
- **La configuration pays est un JSON externe**, désigné par `COUNTRY_CONFIG`,
  sans valeur par défaut et sans aucun fichier pays livré dans le dépôt —
  l'instance française comprise. Le chemin externe doit être celui que tout le
  monde emprunte, sinon il pourrit.
- **La justification des valeurs est de la donnée, pas un commentaire** —
  `nsi_calibration` est écrit par le script de calibration, jamais à la main.
- **Le chargeur valide et échoue au démarrage**, clés inconnues comprises.
- **Pas d'abstraction anticipée** : une dataclass gelée, pas une hiérarchie.
- **`nsi_writable_tags` ne se recopie jamais** ; sans mesure, il se réduit à
  `brand:wikidata`.
- **Le commentaire de changeset ne parle qu'une langue du pays** : celle
  choisie par le contributeur, à défaut `locales[0]`. Jamais une langue
  étrangère au pays, quelle que soit l'interface.
- **Les unités de décision se testent par classes d'équivalence et de
  discrimination**, jamais par échantillon de paires — §8.
- **Les messages de commit restent en français**, le code passe entièrement à
  l'anglais.
