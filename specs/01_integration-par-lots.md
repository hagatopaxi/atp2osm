# Intégration par lots

Document de conception — à relire avant implémentation.

## Objectif

Aujourd'hui une marque n'est intégrable que si elle compte au plus
`MAX_IMPORT_SIZE` correspondances (100). Les grosses enseignes — celles qui
justifient l'outil — sont donc invisibles dans la liste.

On veut découper une marque en **lots d'au plus 200 POIs**, chaque lot étant un
**ensemble de départements entiers**. L'utilisateur intègre les lots les uns
après les autres, sans attendre le rafraîchissement hebdomadaire entre deux
lots.

**Le mécanisme s'applique à toutes les marques, sans exception ni cas
particulier.** Une marque de 12 POIs forme simplement un lot unique contenant
tous ses départements. Il n'existe qu'un seul chemin applicatif et qu'un seul
rendu visuel : « intégrer une marque » n'existe plus, seul « intégrer un lot »
existe, et pour la plupart des marques ce lot est la marque entière.

Contraintes retenues :

- un département n'est jamais coupé en deux (un changeset OpenStreetMap
  correspond à un département, comme aujourd'hui) ;
- le taux de contrôle humain reste de l'ordre de 1 % (voir « Échantillon ») ;
- aucun département ne doit être systématiquement relégué en fin de file du
  seul fait de son numéro.

## Migration préalable — un statut par changeset

**À réaliser avant le reste du travail, et indépendamment de lui.**

### Le conflit

`import_history` mélange aujourd'hui deux niveaux. Une ligne représente une
action humaine d'intégration, mais elle ne porte qu'un seul statut, alors que
l'intégration produit **un changeset par département**, chacun avec son propre
sort. D'où les statuts `partial_*`, qui compensent l'absence de granularité en
qualifiant l'ensemble, et `changeset_ids INTEGER[]`, qui garde les identifiants
sans pouvoir dire lequel a échoué.

Le découpage en lots rend ce mélange intenable : le blocage doit se raisonner
département par département, ce qu'une ligne à statut unique ne permet pas.

### La structure cible

Une ligne d'historique reste **une action humaine d'intégration**. On lui
adjoint une table fille, une ligne par changeset :

```sql
CREATE TABLE import_departements (
    id                 SERIAL PRIMARY KEY,
    import_id          INTEGER NOT NULL REFERENCES import_history(id) ON DELETE CASCADE,
    departement_number TEXT NOT NULL,
    items_count        INTEGER NOT NULL,
    osm_changeset_id   INTEGER,
    status             TEXT NOT NULL CHECK (status IN ('success', 'error_osm_api', 'error_unknown')),
    comment            TEXT
);

CREATE INDEX import_departements_import_id_idx ON import_departements (import_id);
CREATE INDEX import_departements_dpt_idx ON import_departements (departement_number);
```

- `osm_changeset_id` porte l'identifiant du changeset OpenStreetMap ; il est nul
  quand la création du changeset a échoué. Le préfixe le distingue de
  `import_id`, qui est une clé locale ;
- `items_count` fige l'effectif du département au moment de l'intégration : il
  sert à l'affichage de l'historique, où le comptage courant n'a plus de sens ;
- `status` décrit le sort d'un changeset et la contrainte `CHECK` le fige :
  `success`, `error_osm_api`, `error_unknown`. En sont exclus `partial_*`, qui
  ne qualifie qu'une intégration d'ensemble, et `cancelled`, qui désigne un
  abandon avant tout envoi — sans changeset, il n'y a pas de ligne fille.

Cette table est l'unique support du découpage en lots : elle porte le blocage
par département, la trace des effectifs et la preuve visuelle de l'historique.

`import_history.status` devient une valeur **dérivée** de ses enfants, calculée
à l'écriture, et conserve `partial_*` pour désigner un lot où une partie
seulement des départements est passée.

### Reprise de l'existant

Les lignes antérieures ne sont pas migrées : sans détail par changeset, il n'y a
rien à reconstituer — `changeset_ids` garde les identifiants sans dire à quel
département chacun correspondait. Elles restent lisibles avec leur statut
d'ensemble et continuent de bloquer la marque entière. `changeset_ids` est
conservée pour ces lignes ; les nouvelles intégrations la laissent nulle et
remplissent la table fille.

**Ce que cet historique sans lignes filles coûte aux algorithmes : rien.** La
branche « intégration sans changeset » existe de toute façon pour les abandons
(`cancelled`) et pour les marques sans rien à intégrer, et les anciennes lignes
l'empruntent telle quelle — aucune requête ni aucun branchement de plus.

La reprise depuis les logs a en outre détaillé toutes les intégrations
partielles : il ne reste aucune ligne `partial` sans enfants, et aucune
intégration n'en produit plus. La règle des 2 semaines n'a donc jamais eu à
exister ; le cooldown des lignes sans enfants se limite à 4 semaines
(`error`) et 3 mois (`success`). Un abandon (`cancelled`) n'a pas de
cooldown : la marque reste cachée tant qu'aucun de ses spiders ATP n'a été
modifié après l'abandon (date `updated_at` de `atp_spiders`).

## Processus fonctionnels

### 1. Composition d'un lot

Le découpage n'est **jamais persisté** : il est recalculé à chaque visite de la
page de validation, à partir de l'état courant des données. Deux visites
successives sans intégration entre-temps donnent le même lot.

1. Compter les correspondances de la marque, groupées par département.
2. Retirer les départements bloqués (voir « État des données »).
3. Répartir le reste en lots de 200 POIs maximum, algorithme *first-fit
   decreasing* :
   - trier les départements du plus grand au plus petit ;
   - ouvrir un lot avec le plus grand département restant ;
   - y ajouter à répétition le plus grand département qui tient encore dans la
     place disponible ;
   - fermer le lot quand plus aucun ne tient, et recommencer.
4. Proposer le **premier lot** à l'utilisateur.

L'ordre est dicté par la taille, jamais par le code du département : la Creuse
et le Val-d'Oise ont la même chance de partir dans les premiers lots.

Implémentation : `pack_departements(counts, max_size)` dans `src/matching.py`,
couverte par `tests/test_pack_departements.py`.

**Cas limite — un département dépassant à lui seul 200 POIs.** Il constitue un
lot à lui seul et **il est tronqué à 200 POIs**. Les POIs restants ne sont pas
rattrapés : le département étant bloqué par son cooldown, ils repartiront à
l'expiration de celui-ci (3 mois), lors d'un passage ultérieur sur la marque.

Le cas n'appelle pas de traitement particulier car il ne se présente pas : sur
les données actuelles, les 7 091 couples (marque, département) plafonnent à
90 POIs, très loin de la limite. La troncature n'est qu'un filet.

La troncature intervient **avant** le tirage de l'échantillon : l'utilisateur ne
relit que des POIs qui seront effectivement intégrés. Le sous-ensemble retenu
est celui que renvoie la requête de correspondances, sans ordre significatif.

### 2. Échantillon de contrôle

**5 POIs par lot**, tirés au hasard parmi ceux du lot proposé — donc uniquement
parmi ceux qui vont réellement être intégrés. Un lot de moins de 5 POIs est
relu intégralement : `min(n, 5)`.

Un lot valant au plus 200 POIs, ce sont **au minimum 2,5 % des POIs** qui sont
contrôlés, et davantage sur les lots incomplets. La formule proportionnelle
`max(ceil(n / 100), 5)` n'a plus lieu d'être : sur un lot plafonné à 200, elle
valait toujours 5.

Sur une marque de 3 000 POIs, cela donne environ 15 lots × 5 = 75 POIs relus.
C'est le remplissage des lots jusqu'à 200 qui maintient ce taux au plancher :
des lots plus petits augmenteraient mécaniquement la proportion contrôlée.

Les textes qui promettent « 1 % des établissements de la marque »
(`website/templates/docs.html`, `validate.html`) doivent parler d'au moins
2,5 % du lot.

### 3. Intégration

Inchangée : `BulkUpload` groupe les changements par département et crée un
changeset par département. Un lot de 4 départements produit 4 changesets, comme
aujourd'hui. Seul l'ensemble des POIs soumis change.

### 4. Enchaînement des lots

Après intégration, les départements du lot sont marqués comme intégrés. Ils
disparaissent du calcul de l'étape 1, et le lot suivant est immédiatement
disponible : **l'utilisateur peut enchaîner sans attendre le rafraîchissement
hebdomadaire**.

## État des données

### Le problème

Les correspondances proviennent de vues matérialisées rafraîchies une fois par
semaine. Entre l'intégration d'un lot et le rafraîchissement suivant, les POIs
intégrés sont toujours présents dans le jeu de correspondances : sans mémoire,
le lot suivant les reproposerait.

C'est l'historique qui joue ce rôle de mémoire.

### Schéma

L'unité de mémoire est la ligne `import_departements` : un département, son
effectif, son statut. Cette table, introduite par la migration préalable, porte
à la fois le blocage, la composition des lots et l'affichage de l'historique.

### Écriture

**Une ligne `import_history` par action humaine d'intégration**, et une ligne
`import_departements` par département du lot :

- département dont le changeset a abouti → `success`, avec son
  `osm_changeset_id` ;
- département en échec → `error_osm_api` / `error_unknown`, `osm_changeset_id`
  nul.

Un lot abandonné avant envoi (`/report-error`) ne produit **aucune ligne
fille** : rien n'a été tenté, il n'y a pas de changeset à décrire. Le statut
`cancelled` reste au niveau de l'intégration seule.

`BulkUpload` connaît déjà ces ensembles : il boucle par département et suit les
succès dans `uploaded_changes`.

Le statut de la ligne `import_history` est **dérivé** de ses enfants et
conserve toute la nomenclature actuelle, `partial_*` compris : tous en succès →
`success` ; aucun → `error_*` ; mélange → `partial_*`. Un lot dont un seul
département a échoué reste donc affiché comme partiel, ce qui est bien ce qui
s'est passé.

### Lecture — blocages d'une marque

Le blocage se lit à deux niveaux, selon qu'une intégration a produit des
changesets ou non.

**Intégrations avec changesets — blocage par département.** Les durées de
cooldown sont inchangées ; le statut consulté est celui du changeset, pas celui
de l'intégration. Un changeset ayant abouti ou non, il n'existe pas de statut
partiel à ce niveau : la règle des 2 semaines n'a plus d'objet.

```sql
SELECT DISTINCT ic.departement_number
FROM import_departements ic
JOIN import_history ih ON ih.id = ic.import_id
WHERE ih.brand_wikidata = %s
  AND ( (ic.status IN ('error_osm_api','error_unknown') AND ih.import_date > NOW() - INTERVAL '4 weeks')
     OR (ic.status = 'success'                          AND ih.import_date > NOW() - INTERVAL '3 months') )
```

**Intégrations sans changeset — blocage de la marque entière.** Un abandon
(`cancelled`) signale une donnée jugée fautive, pas un département en
particulier : toute la marque s'efface, jusqu'à ce qu'un de ses spiders
ait été modifié après l'abandon — c'est la seule chose qui puisse changer la
donnée. Un spider sans date ne la fait jamais revenir. Une marque sans rien à
intégrer, et les rares lignes antérieures que la reprise n'a pas su détailler,
gardent le cooldown de leur statut. `partial`
n'y figure pas : il suppose des lignes filles, et n'en manque plus aucune.

```sql
SELECT 1
FROM import_history ih
WHERE ih.brand_wikidata = %s
  AND NOT EXISTS (SELECT 1 FROM import_departements ic WHERE ic.import_id = ih.id)
  AND ( <règles de cooldown actuelles, au niveau de l'intégration> )
LIMIT 1
```

Si cette seconde requête renvoie une ligne, la marque n'est pas proposée du
tout et le calcul du lot n'a pas lieu.

Le cooldown ne sert plus qu'à couvrir la fenêtre entre l'intégration et le
rafraîchissement suivant : une fois celui-ci passé, les POIs intégrés ont
disparu des correspondances et il n'y a plus rien à bloquer.

### Lecture — liste des marques

`get_all` doit annoncer, pour chaque marque, le nombre de POIs **actuellement
intégrables** (départements bloqués exclus) et non le total. Sinon la liste
affiche 3 000 POIs pour une marque dont 200 seulement sont accessibles.

Cela suppose un comptage par `(marque, département)`. Pas de seconde vue :
`mv_places_brand` gagne `departement_number` dans son `GROUP BY`, et `get_all`
somme les départements non bloqués. Le total par marque reste dérivable
(`SUM(total)`), donc rien ne se dédouble entre deux objets à garder en phase.

```sql
CREATE MATERIALIZED VIEW mv_places_brand AS
SELECT
    STRING_AGG(DISTINCT atp_brand, ' / ' ORDER BY atp_brand) AS brand,
    atp_brand_wikidata AS brand_wikidata,
    departement_number,
    COUNT(*) AS total
FROM (<MATCHED_POI_SQL>) matched
WHERE is_importable
GROUP BY atp_brand_wikidata, departement_number
```

```sql
WITH blocked AS (
  SELECT ih.brand_wikidata, array_agg(DISTINCT ic.departement_number) AS dpts
  FROM import_departements ic
  JOIN import_history ih ON ih.id = ic.import_id
  WHERE <conditions par changeset, comme ci-dessus>
  GROUP BY ih.brand_wikidata
)
SELECT b.brand_wikidata, MAX(b.brand) AS brand, SUM(b.total) AS available
FROM mv_places_brand b
LEFT JOIN blocked ON blocked.brand_wikidata = b.brand_wikidata
WHERE NOT (b.departement_number = ANY(COALESCE(blocked.dpts, '{}')))
  AND <la marque n'a pas d'intégration bloquante sans changeset>
GROUP BY b.brand_wikidata
```

**Libellé de la marque :** groupé par département, `brand` devient local au
département et n'est plus ré-agrégeable proprement (`STRING_AGG` sur des
libellés déjà agrégés donnerait « A / B / A »). `MAX(b.brand)` suffit : c'est
un libellé d'affichage, prendre la variante d'un département est acceptable.

**Coût :** nul à la construction — le temps est entièrement dans le
`ST_DWithin` de `MATCHED_POI_SQL`, pas dans l'agrégation ; une clé de plus au
`GROUP BY` ne change ni le plan ni le volume lu. À la lecture, la vue passe à
~1 ligne par (marque, département), plafonné à 96 départements par marque —
quelques dizaines de milliers de lignes au pire, une agrégation de quelques
millisecondes.

**En dev :** la vue se reconstruit seule, sans rejouer tout le pipeline —
`python -m src.pipeline step mv-brand` (le `DROP … IF EXISTS` la rend
rejouable à volonté).

Les deux niveaux de blocage se cumulent : les départements bloqués sont exclus
du comptage, et les marques sous blocage global disparaissent de la liste.

**Changement de logique à noter :** `get_all` filtre aujourd'hui sur le *dernier*
import de la marque (`DISTINCT ON`). Avec des lots, il faut considérer *tous* les
imports non périmés — le `DISTINCT ON` disparaît au profit de l'agrégation
ci-dessus.

### Cycle de vie d'un département

```
disponible
   │  intégration réussie
   ▼
bloqué (3 mois)  ──── rafraîchissement hebdo ────►  plus de correspondances
   │                                                 (le blocage devient sans objet)
   │  ou : intégration en échec → bloqué 4 semaines
   │  ou : abandon → toute la marque, toutes vagues, bloquée jusqu'à
   │       modification d'un de ses spiders
   ▼
disponible
```

## Interface

Une seule présentation, quelle que soit la taille de la marque.

### Liste des marques

- Le nombre affiché devient le nombre de POIs **restant à intégrer**
  (départements bloqués exclus). Le total historique de la marque n'est ni connu
  ni utile ici : « 2 400 POIs à intégrer » suffit. C'est aussi ce nombre qui
  ordonne la liste.
- **Le filtre de portée disparaît.** `filter_brands` écarte aujourd'hui les
  marques dont `total > max_import_size` (`scope=importable`, badge
  `count_importable`, message « les intégrations de grande taille ne sont pas
  encore disponibles » dans `brands.html`). Toutes les marques deviennent
  intégrables : ce filtre, ce badge et ce message n'ont plus d'objet.
- Le gabarit `oversized.html` n'est plus atteignable par un dépassement de
  taille — seulement par une divergence réelle de comptage.

### Page de validation

En tête de page, en petits caractères, **le périmètre du lot** : la liste des
départements et le nombre de POIs de chacun, plus le total.

> Ce lot couvre 4 départements — Rhône (78), Nord (61), Gironde (39), Creuse
> (12) — soit 190 POIs.

L'utilisateur sait ainsi exactement ce qu'il intègre, et pour une petite marque
le même bandeau lui confirme que le lot couvre toute la marque.

### Historique

Chaque ligne d'intégration détaille ses départements — nom, effectif, statut,
lien vers le changeset — comme preuve visuelle a posteriori de ce qui a été
intégré. Tout vient de `import_departements`, y compris les effectifs figés.

Le statut d'ensemble reste affiché en tête de ligne ; `partial_*` y prend son
sens plein, puisque le détail montre alors quel département est passé et lequel
a échoué.

Une intégration partielle affiche en outre **son taux de réussite** à côté de
son statut, compté en départements : « partiel — 4/5 ». La valeur se déduit des
lignes filles (`success` sur total). Les intégrations antérieures à la
migration n'ont pas de lignes filles : elles gardent le statut `partial_*` seul,
sans taux — d'où la mention « si connu ».

### URL

`/brands/<brand_wikidata>/validate` inchangée, sans département. Le lot est
recalculé à chaque visite. À arbitrer si un lot explicite dans l'URL est
préféré.

## Hors périmètre

- Rattrapage immédiat du reliquat d'un département de plus de 200 POIs : il
  attend son cooldown.
- Reprise des lignes `import_history` antérieures : sans détail par changeset,
  il n'y a rien à reconstituer.
- Purge des lignes devenues sans objet après rafraîchissement — le cooldown les
  périme seul.

## Ordre d'implémentation

0. **Migration préalable** : table `import_departements`, écriture d'une ligne par
   changeset dans `upload_changes`, statut d'historique dérivé, affichage du
   détail dans l'historique. Livrable autonome, sans lien avec les lots — **fait**.
1. `pack_departements` + tests — **fait**.
2. Composition du lot à partir des départements non bloqués — **fait**.
3. Blocage par département dans `_get_blocking_import`, composition et
   troncature du lot dans `brands_validate` / `brands_confirm` /
   `upload_changes`. Le garde-fou « comptage incohérent » qui met la marque en
   erreur au-delà de la limite ne peut plus se déclencher via un département
   surdimensionné : il ne surveille plus qu'une divergence réelle entre les
   deux comptages — **fait**.
4. `departement_number` dans `mv_places_brand` et nouvelle requête `get_all` — **fait**.
5. Interface : bandeau de périmètre sur `validate`, départements dans
   l'historique, retrait du filtre de portée sur la liste des marques — **fait**.
6. Textes de documentation (`docs.html`, `validate.html` : « 1 % du lot ») — **fait**.
