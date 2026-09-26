# Vagues d'intégration et protection des modifications humaines récentes

## Vagues

Les données ATP d'un spider sont republiées tous les 3 mois. Entre deux
publications, une marque est traitée en **vagues** : une vague est une
typologie d'intégration, et elle couvre toutes les subdivisions avant que la
suivante commence.

| # | Vague | État | Taille de lot |
|---|---|---|---|
| 1 | Ajout de tags absents sur des POIs existants | livrée | 100 |
| 2 | Modification de tags existants sur des POIs existants | alpha | 1 |
| 3 | Ajout de nouveaux POIs | hors périmètre, non étudiée | — |

Règles :

- **Une vague à la fois, par marque.** La vague courante d'une marque est la
  première qui a encore des correspondances intégrables. Tant qu'une seule
  subdivision reste à traiter en vague 1, la vague 2 n'est pas proposée.
- **Le découpage géographique est celui des lots** (spec 01) : subdivisions
  entières, un changeset par subdivision, cooldown par subdivision. Une vague
  est finie quand aucune de ses subdivisions n'est intégrable — soit qu'elles
  aient été intégrées, soit qu'elles soient sous cooldown.
- **La vague suivante attend les données OSM.** Une subdivision intégrée en
  vague N reste bloquée pour les vagues suivantes tant que les données OSM
  locales sont antérieures à l'intégration : les correspondances de la vague
  N+1 ont été calculées sur les objets que la vague N vient de modifier, et
  leur envoi entrerait en conflit avec les versions qu'elle a créées. Les
  autres subdivisions n'attendent pas : un objet OSM ne correspond qu'à un
  seul POI ATP par marque, donc les deux vagues le placent dans la même
  subdivision. La date comparée est celle des données (horodatage
  Geofabrik), pas celle de l'import : en pratique, la vague suivante revient
  au rafraîchissement du lendemain. Une marque dont il ne reste que des
  correspondances bloquées n'est pas close : `/validate` affiche une page
  d'attente et n'écrit rien.
- **Chaque vague a sa propre taille de lot.** `pack_subdivisions` reçoit la
  taille de la vague courante ; il n'y a pas de constante unique.
- **Le nombre de vagues n'est pas figé.** Une vague est une entrée d'une liste
  ordonnée — numéro, prédicat de correspondance, taille de lot, libellé.
  Ajouter la vague 3 consiste à ajouter une entrée, pas à toucher
  l'enchaînement.

### État

`import_history` gagne une colonne `wave SMALLINT NOT NULL`. Elle sert à
l'affichage et à cantonner le cooldown à sa vague : une subdivision intégrée
en vague 1 ne doit pas bloquer la vague 2 de la même marque au-delà du
prochain rafraîchissement OSM. Les requêtes de blocage de la spec 01 gagnent
donc `AND ih.wave = %s`.

Rien d'autre n'est persisté : la vague courante se déduit des correspondances
restantes, comme le lot se déduit des subdivisions non bloquées. Les lignes
antérieures à la migration valent `wave = 1`.

### Interface

La page de validation annonce la vague en cours à côté du périmètre du lot,
et la liste des marques indique laquelle est proposée. Une vague en alpha le
dit explicitement.

## Vague 2 — ne jamais écraser une valeur humaine récente

ATP propose des tags issus des sites web des enseignes. Quand un mappeur a
renseigné à la main un `opening_hours` ou un `phone` la semaine dernière, sa
valeur est un choix délibéré, souvent vérifié sur le terrain : elle prime sur
la nôtre. À l'inverse, une valeur posée par un bot n'a pas été vérifiée par
qui que ce soit et peut être remplacée.

La règle, tag par tag :

- **modification ancienne** (au-delà du seuil) → on écrit
- **modification récente par un bot** → on écrit
- **modification récente par un humain** → on n'écrit pas ce tag

Le seuil de fraîcheur est un nombre de semaines, à fixer par configuration.

La protection est **par tag**, pas par objet. Un POI dont le `name` a été
corrigé hier reste éligible à recevoir un `website` d'ATP.

## Données nécessaires

| Donnée | Source | Coût |
|---|---|---|
| Date de dernière modif de l'objet | PBF Geofabrik, colonne `osm_timestamp` | gratuit, déjà en base |
| Versions successives d'un objet, avec tags et auteur | `GET /api/0.6/<type>/<id>/history` | 1 requête par objet |
| Tag `bot` du changeset | `GET /api/0.6/changeset/<id>` | 1 requête par changeset |

Ce que ces sources ne donnent **pas** :

- Aucune source, nulle part, ne date un tag individuellement. OSM ne stocke
  qu'un timestamp par objet. La date d'un tag se reconstruit en comparant les
  versions successives — il n'y a pas d'autre voie.
- Les extraits Geofabrik publics ne portent ni `changeset`, ni `uid`, ni
  `user` (vérifié : ces champs valent 0 / chaîne vide). Seuls `version` et
  `osm_timestamp` en sortent. L'auteur ne peut venir que de l'API.

## Algorithme

Appliqué au moment de la validation, sur le lot de POIs en cours.

```
pour chaque POI du lot :
    si osm_timestamp < NOW - seuil :
        écrire tous les tags du diff        # cas majoritaire, 0 requête
        continuer

    historique = GET /api/0.6/<type>/<id>/history

    pour chaque tag que le diff veut écrire :
        remonter les versions de la plus récente vers la plus ancienne
        jusqu'à trouver celle qui a posé la valeur actuelle
        -> date + changeset de cette version

        si date < NOW - seuil :          écrire
        sinon si changeset est un bot :  écrire
        sinon :                          ne pas écrire ce tag
```

Deux filtres en amont font tout le travail d'économie : le `osm_timestamp`
écarte la grande majorité des POIs sans aucune requête, et la restriction aux
seuls tags que le diff veut écrire évite de dater des tags dont on n'a que
faire.

### Identifier un bot

`bot=yes` sur le changeset est le seul marqueur retenu. Tout le reste est
traité comme humain.

Le nom d'utilisateur n'est pas un signal : OSM n'impose aucune convention de
nommage et beaucoup d'imports tournent sous un compte ordinaire. `created_by`
non plus : il nomme l'outil, pas la nature de l'édition.

Conséquence assumée : un bot qui ne se déclare pas est traité comme un humain,
et sa valeur est préservée. Le doute profite à l'existant.

### Remonter à la version qui a posé la valeur

Une seule règle : en partant de la version courante et en descendant, la
réponse est **la plus ancienne version consécutive portant la valeur
actuelle**. C'est celle qui l'a posée.

L'absence de tag est une valeur comme une autre dans cette comparaison. Une
version qui réécrit la même valeur ne rompt donc pas la suite, et une valeur
recréée à l'identique après suppression la rompt bien — la suppression est une
valeur différente. Un objet à une seule version répond v1.

## Volumétrie et limites externes

- Le lot de vague 2 vaut 1 POI en alpha : au plus une requête d'historique et
  une de changeset par validation. Les chiffres ci-dessous valent pour la
  taille de lot relevée ensuite.
- Le nombre de requêtes par lot est celui des POIs qui survivent au filtre
  `osm_timestamp`, plus un par changeset récent rencontré. Cette proportion
  n'est pas mesurée : elle dépend du seuil retenu et de l'activité réelle sur
  les POIs concernés. Le plafond absolu reste borné par la taille du lot.
- L'API OSM ne publie aucune limite de fréquence en lecture : ni en-tête
  `X-RateLimit-*`, ni `Retry-After`. `/api/capabilities` ne donne que des
  limites de taille. La règle est un usage raisonnable : séquentiel, et un
  `User-Agent` identifiant l'application.
- Les lectures passent par un CDN, elles n'atteignent pas toutes la base.

## Statistiques

Une modification doit se compter comme un ajout : les mêmes écritures, au même
moment, avec la vague en plus. `get_stats` produit déjà, pour un lot, le
nombre de POIs, le total de tags touchés, le détail par tag et par subdivision
— la vague 2 s'y branche sans code de comptage propre.

Ce qui est enregistré, par intégration :

- la ligne `import_history`, avec sa colonne `wave` : elle sépare les
  statistiques d'ajout de celles de modification ;
- une ligne `import_subdivisions` par changeset, avec son `items_count` figé,
  comme en vague 1.

**Un ajout par rapport à la vague 1 : le détail par tag est persisté.**
`import_subdivisions` gagne une colonne `tag_counts JSONB` — `{"phone": 12,
"website": 7}` — écrite au moment de l'intégration. Aujourd'hui ce détail
n'existe qu'à l'écran, recalculé depuis les correspondances ; après le
rafraîchissement suivant, la correspondance a disparu et le compte n'est plus
reconstituable. Pour un ajout la perte est bénigne ; pour une modification,
elle retire la seule mesure qui intéresse : quel tag a été remplacé, et
combien de fois. La colonne est remplie pour les deux vagues, l'écriture étant
la même.

Cela suffit à répondre plus tard, par vague, par marque, par subdivision et
par tag : combien de POIs touchés, combien de tags écrits, lesquels. Aucune
table de statistiques, aucun agrégat précalculé : les volumes sont ceux de
l'historique, une requête les résume.

## Impact sur le pipeline

`osm2pgsql` tourne avec `-x` et `generic.lua` remplit `osm_timestamp`
(epoch, `int8` — le flex output n'a pas de type `timestamp`). `mv_places`
expose la colonne convertie par `to_timestamp()`.

La colonne n'est peuplée qu'après un import osm2pgsql complet. Note au
passage : `version` était déjà déclarée dans le style mais sortait à zéro,
faute de `-x`.

## Interface de la vague 2

Une modification remplace une valeur déjà présente dans OSM ; c'est un geste
plus engageant que l'ajout d'un tag absent. L'interface doit le refléter.

**Lot de taille 1 en alpha.** C'est la taille de lot de la vague, pas une
exception : la vague 1 continue de livrer par 100. La valeur est provisoire,
le temps d'observer les premiers retours de la communauté ; elle est relevée
ensuite.

**Un exemple par tag modifié dans la liste des validations.** Pour chaque tag
que le lot veut modifier, la liste montre un cas concret — la valeur OSM
actuelle et la valeur ATP proposée — et non le seul nom du tag. On doit
pouvoir juger de la pertinence d'une modification sans ouvrir l'écran de
validation.

**Badge de vague sur les intégrations.** Visible dans la liste et dans
l'historique, il distingue au premier coup d'œil une intégration qui ne fait
qu'ajouter des tags d'une qui en remplace.

**Avant/après lisible à l'écran de validation.** Les valeurs actuelle et
proposée se lisent côte à côte, alignées, sans avoir à les chercher dans le
reste des tags de l'objet. Les tags ajoutés et les tags remplacés se
distinguent visuellement.

## Hors périmètre

- Les tags écartés par la protection : on n'enregistre que ce qui est
  intégré, pas ce à quoi on a renoncé.
- La vague 3 (ajout de nouveaux POIs) : seule sa place dans l'enchaînement est
  réservée, son contenu n'est pas étudié.
- Reconstituer l'évolution complète d'un tag dans le temps. On ne cherche
  qu'une date, celle de la valeur actuelle.
- Les extraits `osm-internal.download.geofabrik.de`, qui portent `user` et
  `changeset` mais imposent une authentification OSM dans le pipeline. Les
  quelques requêtes API par lot rendent ce coût inutile.
- Le dump planet des changesets (~6 Go), pour la même raison.
