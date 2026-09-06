"""Compare the phone keys before and after migration 022, on real data.

The unit tests prove the function behaves; this proves the corpus does not
move. Run it on a database cloned from production, before deploying:

    OSM_DB_NAME=… OSM_DB_USER=… OSM_DB_PASSWORD=… OSM_DB_HOST=… OSM_DB_PORT=… \
        uv run python scripts/check_phone_normalization.py

It only reads, and it installs the legacy function under its own name in a
temporary schema that it drops on the way out.
"""

import pathlib
import sys

import psycopg

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from src.config import get_database  # noqa: E402

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
LEGACY_SQL = MIGRATIONS / "012_normalize_phone_fn.sql"
SCHEMA = "phone_check"

# Both sides of the join, as they are named today.
TABLES = (("atp_places", "phone"), ("mv_places", "phone"))


def pairs_matched_on_phone_only(cur, function):
    """POI pairs the phone alone brings together — the number that must hold."""
    cur.execute(
        f"""
        SELECT count(*) FROM mv_places osm
        JOIN atp_places atp
          ON {function}(osm.phone) = {function}(atp.phone)
         AND ST_DWithin(osm.geom::geography,
                        ST_GeomFromGeoJSON(atp.geom)::geography, 500)
        WHERE osm.brand_wikidata IS DISTINCT FROM atp.brand_wikidata
          AND LOWER(osm.brand) IS DISTINCT FROM LOWER(atp.brand)
          AND LOWER(osm.name)  IS DISTINCT FROM LOWER(atp."name")
        """
    )
    return cur.fetchone()[0]


def collisions(cur, table, column, function):
    """Distinct written values collapsing onto one key."""
    cur.execute(
        f"""
        SELECT count(*) FROM (
            SELECT {function}({column}) AS key, count(DISTINCT {column}) AS n
            FROM {table} WHERE {column} IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT {column}) > 1
        ) t
        """
    )
    return cur.fetchone()[0]


def refused(cur, table, column):
    cur.execute(
        f"""SELECT count(*) FROM {table}
            WHERE {column} IS NOT NULL AND normalize_phone({column}) IS NULL"""
    )
    return cur.fetchone()[0]


def sample_refused(cur, table, column, limit=20):
    cur.execute(
        f"""SELECT DISTINCT {column} FROM {table}
            WHERE {column} IS NOT NULL AND normalize_phone({column}) IS NULL
            LIMIT {limit}"""
    )
    return [row[0] for row in cur.fetchall()]


def main():
    conn = psycopg.connect(**get_database().connect_kwargs)
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        cur.execute(f"SET search_path TO public, {SCHEMA}")
        cur.execute(
            LEGACY_SQL.read_text().replace(
                "FUNCTION normalize_phone(", f"FUNCTION {SCHEMA}.legacy_normalize_phone("
            )
        )

        before = pairs_matched_on_phone_only(cur, f"{SCHEMA}.legacy_normalize_phone")
        after = pairs_matched_on_phone_only(cur, "normalize_phone")
        drift = abs(after - before) / before * 100 if before else 0.0
        print(f"pairs matched on phone alone: {before} → {after} "
              f"({drift:.2f} % drift)")
        if drift > 1:
            print("  DRIFT ABOVE 1 % — inspect before deploying")

        for table, column in TABLES:
            legacy = collisions(cur, table, column, f"{SCHEMA}.legacy_normalize_phone")
            current = collisions(cur, table, column, "normalize_phone")
            print(f"{table}.{column}: keys shared by several writings "
                  f"{legacy} → {current}")
            if current > legacy:
                print("  COLLISIONS ARE GROWING — inspect before deploying")

            n = refused(cur, table, column)
            print(f"{table}.{column} : valeurs devenues NULL : {n}")
            for value in sample_refused(cur, table, column):
                print(f"    {value!r}")

        cur.execute(f"DROP SCHEMA {SCHEMA} CASCADE")


if __name__ == "__main__":
    main()
