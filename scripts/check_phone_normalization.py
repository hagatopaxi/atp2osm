"""Compare the phone keys before and after migration 022, on real data.

The unit tests prove the function behaves; this proves the corpus does not
move. Run it on a database cloned from production, before deploying:

    OSM_DB_NAME=… OSM_DB_USER=… OSM_DB_PASSWORD=… OSM_DB_HOST=… OSM_DB_PORT=… \
        uv run python scripts/check_phone_normalization.py

It only reads, and it installs the legacy function in a schema of its own,
which it drops on the way out.
"""

import pathlib
import sys
from typing import Any, Final, LiteralString

import psycopg
from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from src.config import get_database
from src.db import sql_file

Cursor = psycopg.Cursor[Any]

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
LEGACY_SQL = MIGRATIONS / "012_normalize_phone_fn.sql"
SCHEMA: Final = "phone_check"
LEGACY_FUNCTION: Final = f"{SCHEMA}.normalize_phone"

# Both sides of the join, as they are named today.
TABLES = (("atp_places", "phone"), ("mv_places", "phone"))


def _count(cur: Cursor) -> int:
    row = cur.fetchone()
    return int(row[0]) if row else 0


def pairs_matched_on_phone_only(cur: Cursor, function: LiteralString) -> int:
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
        """  # noqa: S608 — the function name is a constant of this script
    )
    return _count(cur)


def collisions(cur: Cursor, table: str, column: str, function: LiteralString) -> int:
    """Distinct written values collapsing onto one key."""
    cur.execute(
        sql.SQL("""
        SELECT count(*) FROM (
            SELECT {function}({column}) AS key, count(DISTINCT {column}) AS n
            FROM {table} WHERE {column} IS NOT NULL
            GROUP BY 1 HAVING count(DISTINCT {column}) > 1
        ) t
        """).format(
            function=sql.SQL(function), column=sql.Identifier(column), table=sql.Identifier(table)
        )
    )
    return _count(cur)


def refused(cur: Cursor, table: str, column: str) -> int:
    cur.execute(
        sql.SQL("""SELECT count(*) FROM {table}
            WHERE {column} IS NOT NULL AND normalize_phone({column}) IS NULL""").format(
            column=sql.Identifier(column), table=sql.Identifier(table)
        )
    )
    return _count(cur)


def sample_refused(cur: Cursor, table: str, column: str, limit: int = 20) -> list[str]:
    cur.execute(
        sql.SQL("""SELECT DISTINCT {column} FROM {table}
            WHERE {column} IS NOT NULL AND normalize_phone({column}) IS NULL
            LIMIT {limit}""").format(
            column=sql.Identifier(column), table=sql.Identifier(table), limit=sql.Literal(limit)
        )
    )
    return [str(row[0]) for row in cur.fetchall()]


def main() -> None:
    conn = psycopg.connect(get_database().conninfo)
    with conn, conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        # The migration creates an unqualified normalize_phone: with SCHEMA
        # first on the path it lands there, beside the live one in public.
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql_file(LEGACY_SQL))
        cur.execute(f"SET search_path TO public, {SCHEMA}")

        before = pairs_matched_on_phone_only(cur, LEGACY_FUNCTION)
        after = pairs_matched_on_phone_only(cur, "normalize_phone")
        drift = abs(after - before) / before * 100 if before else 0.0
        print(f"pairs matched on phone alone: {before} → {after} ({drift:.2f} % drift)")
        if drift > 1:
            print("  DRIFT ABOVE 1 % — inspect before deploying")

        for table, column in TABLES:
            legacy = collisions(cur, table, column, LEGACY_FUNCTION)
            current = collisions(cur, table, column, "normalize_phone")
            print(f"{table}.{column}: keys shared by several writings {legacy} → {current}")
            if current > legacy:
                print("  COLLISIONS ARE GROWING — inspect before deploying")

            n = refused(cur, table, column)
            print(f"{table}.{column}: values now NULL: {n}")
            for value in sample_refused(cur, table, column):
                print(f"    {value!r}")

        cur.execute(f"DROP SCHEMA {SCHEMA} CASCADE")


if __name__ == "__main__":
    main()
