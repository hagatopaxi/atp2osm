"""Backfill: give every history line the subdivision name it was created with.

Until now the name was looked up in a hard-coded dict at display time. That
dict is gone — names come from OSM boundaries — and a code can be split by a
redistricting (20 -> 2A/2B, 69 -> 69D/69M) or simply renamed. A history line
records what happened, so it carries its own name and joins nothing.

The backfill is exact, not a heuristic: it applies the very dict the display
used — fallback included — to codes that do not change.

Not read from the `subdivisions` table, for three reasons. It can be empty when
migrations run, since they precede the first OSM import. Three of the 99
historic codes have no boundary at all — `20` (Corse, now 2A and 2B), `972`
(now 972R) and `980` (Monaco, which the postcode derivation invented). And a
code is not unique across levels: joining `75` back gives both Paris and
Nouvelle-Aquitaine.
"""

import logging

from src.migrate import Migration

logger = logging.getLogger(__name__)


# Frozen copy of src/matching.py's DEPARTEMENT_NAMES, as it stood the day the
# dict was deleted. A migration is a snapshot: it must keep working once the
# code it came from is gone.
_DEPARTEMENT_NAMES = {
    "01": "Ain",
    "02": "Aisne",
    "03": "Allier",
    "04": "Alpes-de-Haute-Provence",
    "05": "Hautes-Alpes",
    "06": "Alpes-Maritimes",
    "07": "Ardèche",
    "08": "Ardennes",
    "09": "Ariège",
    "10": "Aube",
    "11": "Aude",
    "12": "Aveyron",
    "13": "Bouches-du-Rhône",
    "14": "Calvados",
    "15": "Cantal",
    "16": "Charente",
    "17": "Charente-Maritime",
    "18": "Cher",
    "19": "Corrèze",
    "20": "Corse",
    "2A": "Corse-du-Sud",
    "2B": "Haute-Corse",
    "21": "Côte-d'Or",
    "22": "Côtes-d'Armor",
    "23": "Creuse",
    "24": "Dordogne",
    "25": "Doubs",
    "26": "Drôme",
    "27": "Eure",
    "28": "Eure-et-Loir",
    "29": "Finistère",
    "30": "Gard",
    "31": "Haute-Garonne",
    "32": "Gers",
    "33": "Gironde",
    "34": "Hérault",
    "35": "Ille-et-Vilaine",
    "36": "Indre",
    "37": "Indre-et-Loire",
    "38": "Isère",
    "39": "Jura",
    "40": "Landes",
    "41": "Loir-et-Cher",
    "42": "Loire",
    "43": "Haute-Loire",
    "44": "Loire-Atlantique",
    "45": "Loiret",
    "46": "Lot",
    "47": "Lot-et-Garonne",
    "48": "Lozère",
    "49": "Maine-et-Loire",
    "50": "Manche",
    "51": "Marne",
    "52": "Haute-Marne",
    "53": "Mayenne",
    "54": "Meurthe-et-Moselle",
    "55": "Meuse",
    "56": "Morbihan",
    "57": "Moselle",
    "58": "Nièvre",
    "59": "Nord",
    "60": "Oise",
    "61": "Orne",
    "62": "Pas-de-Calais",
    "63": "Puy-de-Dôme",
    "64": "Pyrénées-Atlantiques",
    "65": "Hautes-Pyrénées",
    "66": "Pyrénées-Orientales",
    "67": "Bas-Rhin",
    "68": "Haut-Rhin",
    "69": "Rhône",
    "70": "Haute-Saône",
    "71": "Saône-et-Loire",
    "72": "Sarthe",
    "73": "Savoie",
    "74": "Haute-Savoie",
    "75": "Paris",
    "76": "Seine-Maritime",
    "77": "Seine-et-Marne",
    "78": "Yvelines",
    "79": "Deux-Sèvres",
    "80": "Somme",
    "81": "Tarn",
    "82": "Tarn-et-Garonne",
    "83": "Var",
    "84": "Vaucluse",
    "85": "Vendée",
    "86": "Vienne",
    "87": "Haute-Vienne",
    "88": "Vosges",
    "89": "Yonne",
    "90": "Territoire de Belfort",
    "91": "Essonne",
    "92": "Hauts-de-Seine",
    "93": "Seine-Saint-Denis",
    "94": "Val-de-Marne",
    "95": "Val-d'Oise",
    # Overseas départements and regions (DROM)
    "971": "Guadeloupe",
    "972": "Martinique",
    "973": "Guyane",
    "974": "La Réunion",
    "976": "Mayotte",
    # Overseas collectivities (COM)
    "975": "Saint-Pierre-et-Miquelon",
    "977": "Saint-Barthélemy",
    "978": "Saint-Martin",
    "984": "Terres australes et antarctiques françaises",
    "986": "Wallis-et-Futuna",
    "987": "Polynésie française",
    "988": "Nouvelle-Calédonie",
}


class BackfillSubdivisionNames(Migration):
    def migrate(self) -> None:
        with self.conn.cursor() as cursor:
            cursor.execute(
                """SELECT DISTINCT subdivision_code FROM import_subdivisions
                   WHERE subdivision_name IS NULL"""
            )
            codes = [row[0] for row in cursor.fetchall()]

            # A code the dict never named falls back on itself — which is
            # exactly what the display did, `DEPARTEMENT_NAMES.get(code, code)`.
            # The postcode derivation produced a few of those: 980 is Monaco,
            # which is not a French département and never had a name here.
            unknown = sorted(c for c in codes if c not in _DEPARTEMENT_NAMES)
            if unknown:
                logger.info("No name for %s, keeping the code", ", ".join(unknown))

            for code in codes:
                cursor.execute(
                    """UPDATE import_subdivisions SET subdivision_name = %s
                       WHERE subdivision_code = %s AND subdivision_name IS NULL""",
                    (_DEPARTEMENT_NAMES.get(code, code), code),
                )

            cursor.execute(
                "ALTER TABLE import_subdivisions ALTER COLUMN subdivision_name SET NOT NULL"
            )

        logger.info("Named %d distinct subdivision code(s)", len(codes))
