# Page « Spiders »

Une page `/spiders` liste les spiders All The Places qui produisent des POI du
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
| Marques | marques (`brand:wikidata`) distinctes que le spider produit ; lien vers `/brands` filtré sur son code Wikidata | spider |
| Run ATP | `errors == 0` du run ATP → succès, sinon erreurs, avec un lien vers le log du spider pour ce run | spider |
| Dernière modification | date du dernier commit touchant le fichier du spider | spider |
| Scrapés | POI du spider dans `atp_places` (pays et territoires) | spider |
| Correspondances | POI du spider ayant rencontré un objet OSM (`mv_places_spider`), avant tout filtre d'intégrabilité | spider |
| Taux de correspondance | correspondances / scrapés | spider |
| Statut atp2osm | statut de la dernière intégration parmi les marques du spider | marque |
| Raisons d'annulation | si ce statut est une annulation : raisons et commentaires saisis au rejet | marque |
| Dernière intégration | sa date, lien vers son détail dans l'historique | marque |

Toutes les colonnes chiffrées comptent des POI ATP, par spider : chaque POI
porte son `spider_id`. L'historique, lui, se compte par marque :
`import_history` ne connaît pas le spider. Un spider emprunte donc la
dernière intégration des marques qu'il produit ; une marque produite par deux
spiders (un agrégateur et la marque elle-même) la prête aux deux.

Chaque en-tête chiffré ou peu évident porte une infobulle (i) d'une phrase ;
Spider, Marques, Raisons d'annulation et Dernière intégration s'en passent.

## Données

- La date de dernière modification vient d'un clone partiel
  (`--filter=blob:none`) du dépôt alltheplaces, conservé dans `data/atp/` et
  mis à jour à chaque run. Un `git log --name-only` sur l'historique date
  tous les fichiers en une passe. GitHub injoignable coûte les dates du run,
  pas le run.
- Le lien du log se déduit du run téléchargé :
  `<dossier du run>/logs/<spider>.txt`, stocké dans `atp_spiders.log_url`.
- `mv_places_spider` est construite et permutée avec `mv_places_brand`, sur
  la même signature : même jointure, mêmes entrées.

## Présentation

Comme les autres listes : tableau, tri par colonne, défilement infini,
filtres texte (spider, marque, code Wikidata), run ATP, statut atp2osm,
raison d'annulation (parmi celles présentes) et période de dernière
intégration. Tri par défaut : dernière modification,
la plus récente d'abord.
