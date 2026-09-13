# Page « Spiders »

Une page `/spiders` liste les spiders AllThePlaces qui produisent des POI du
pays servi, pour répondre à deux questions distinctes que l'orientation
« marque » du reste du site ne couvre pas :

- **le gisement** : ce que chaque spider collecte sur le territoire, et ce qui
  y rencontre un objet OpenStreetMap ;
- **la maintenance** : quels spiders méritent une mise à jour, typiquement ceux
  dont la dernière intégration a été annulée par un contributeur.

## Colonnes

| Colonne | Source | Grain |
|---|---|---|
| Spider | `atp_spiders.spider`, lien vers le fichier sur GitHub | spider |
| Marques | marques (`brand:wikidata`) distinctes que le spider produit | spider |
| Dernier run | `errors == 0` du run ATP → succès, sinon erreurs | spider |
| Dernière modification | date du dernier commit touchant le fichier du spider | spider |
| Collectés ici | POI du spider dans `atp_places` (pays et territoires) | spider |
| Correspondances | POI du spider ayant rencontré un objet OSM (`mv_places_spider`), avant tout filtre d'intégrabilité | spider |
| Intégrés | somme de `items_count` des intégrations des marques du spider | marque |
| Statut précédent | statut de la dernière intégration parmi les marques du spider | marque |
| Dernière intégration | sa date | marque |

Le gisement se compte par spider : chaque POI ATP porte son `spider_id`.
L'historique se compte par marque : `import_history` ne connaît pas le
spider. Un spider emprunte donc les intégrations des marques qu'il produit.
Une marque produite par deux spiders (un agrégateur et la marque elle-même)
compte chez les deux — c'est une somme simple, assumée. Le jour où ce partage
induit en erreur, le remède est d'estampiller `spider_id` sur
`import_history` à l'envoi.

## Données

- La date de dernière modification vient d'un clone partiel
  (`--filter=blob:none`) du dépôt alltheplaces, conservé dans `data/atp/` et
  mis à jour à chaque run. Un `git log --name-only` sur l'historique date
  tous les fichiers en une passe. GitHub injoignable coûte les dates du run,
  pas le run.
- `mv_places_spider` est construite et permutée avec `mv_places_brand`, sur
  la même signature : même jointure, mêmes entrées.

## Présentation

Comme les autres listes : tableau, tri par colonne, défilement infini,
filtres texte (spider, marque, code Wikidata), dernier run, statut précédent
et période de dernière intégration. Tri par défaut : dernière modification,
la plus récente d'abord.
