"""Renaming and backfill of the history: migrations 022 and 023.

Needs the local PostGIS (podman-compose up -d); skipped otherwise.

A history row records what happened. The renaming must therefore move every
row untouched, and the backfill must give each one the name the display used
to look up — no row created, none lost, no count moved.
"""

import pathlib
from collections.abc import Iterator
from types import ModuleType
from typing import LiteralString

import psycopg
import pytest
from psycopg.rows import DictRow, dict_row

from src.config import Database
from src.db import sql_file
from src.migrate import discover_migrations
from tests.conftest import Connection, load_module, one

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
SCHEMA: LiteralString = "test_subdivision_migration"

ROWS = [
    # code, items_count, status — codes that stay, and codes that will split
    ("75", 12, "success"),
    ("13", 7, "success"),
    ("20", 5, "success"),
    ("69", 3, "error_osm_api"),
    ("972", 1, "success"),
    ("988", 2, "success"),
    # A code the old dict never named: the postcode derivation gave 980 to
    # Monaco, and the display fell back on showing the code.
    ("980", 4, "success"),
]


def _sql_migrations() -> list[tuple[int, pathlib.Path]]:
    return [(v, p) for v, p in discover_migrations() if p.suffix == ".sql"]


@pytest.fixture
def conn(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"SET search_path TO {SCHEMA}")
        for version, path in sorted(_sql_migrations()):
            if version > 15:
                break
            c.execute(sql_file(path))
        c.execute(
            """INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, status)
               VALUES ('Q1', 'Chez Michel', 42, 'success') RETURNING id"""
        )
        import_id = one(c.execute("SELECT MAX(id) FROM import_history").fetchone())[0]
        c.cursor().executemany(
            """INSERT INTO import_departements
                   (import_id, departement_number, items_count, status)
               VALUES (%s, %s, %s, %s)""",
            [(import_id, code, count, status) for code, count, status in ROWS],
        )
        c.commit()
        yield c
        c.rollback()
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.commit()


def apply_022(conn: Connection) -> None:
    _version, path = next((v, p) for v, p in _sql_migrations() if p.name.startswith("022_"))
    conn.execute(sql_file(path))
    conn.commit()


def apply_023(conn: Connection) -> ModuleType:
    module = load_module(next(MIGRATIONS.glob("023_*.py")))
    module.BackfillSubdivisionNames(conn).migrate()
    conn.commit()
    return module


def rows(conn: Connection) -> list[DictRow]:
    return (
        conn.cursor(row_factory=dict_row)
        .execute("SELECT * FROM import_subdivisions ORDER BY subdivision_code")
        .fetchall()
    )


def test_the_renaming_moves_every_row_untouched(conn: Connection) -> None:
    before = (
        conn.cursor(row_factory=dict_row)
        .execute("SELECT * FROM import_departements ORDER BY departement_number")
        .fetchall()
    )
    apply_022(conn)
    after = rows(conn)

    assert len(after) == len(before) == len(ROWS)
    for old, new in zip(before, after, strict=False):
        assert new["subdivision_code"] == old["departement_number"]
        assert new["items_count"] == old["items_count"]
        assert new["status"] == old["status"]
        assert new["id"] == old["id"]
        assert new["import_id"] == old["import_id"]


def test_the_backfill_names_every_row_and_moves_no_count(conn: Connection) -> None:
    apply_022(conn)
    module = apply_023(conn)
    after = rows(conn)

    assert len(after) == len(ROWS)
    assert sum(r["items_count"] for r in after) == sum(c for _, c, _ in ROWS)
    for row in after:
        # exactly what the deleted DEPARTEMENT_NAMES gave for that code,
        # fallback included — that is what the page used to print
        code = row["subdivision_code"]
        assert row["subdivision_name"] == module._DEPARTEMENT_NAMES.get(code, code)


def test_no_history_row_is_orphaned_or_duplicated(conn: Connection) -> None:
    apply_022(conn)
    apply_023(conn)
    orphans = one(
        conn.cursor(row_factory=dict_row)
        .execute(
            """SELECT COUNT(*) AS n FROM import_subdivisions s
           WHERE NOT EXISTS (SELECT 1 FROM import_history h WHERE h.id = s.import_id)"""
        )
        .fetchone()
    )["n"]
    duplicates = one(
        conn.cursor(row_factory=dict_row)
        .execute(
            """SELECT COUNT(*) AS n FROM (
               SELECT import_id, subdivision_code FROM import_subdivisions
               GROUP BY 1, 2 HAVING COUNT(*) > 1) d"""
        )
        .fetchone()
    )["n"]
    assert (orphans, duplicates) == (0, 0)


def test_the_backfill_is_idempotent(conn: Connection) -> None:
    apply_022(conn)
    apply_023(conn)
    before = rows(conn)
    apply_023(conn)
    assert rows(conn) == before


def test_an_unknown_code_keeps_itself_as_a_name(conn: Connection) -> None:
    """The display did the same: DEPARTEMENT_NAMES.get(code, code)."""
    apply_022(conn)
    conn.execute(
        """INSERT INTO import_subdivisions (import_id, subdivision_code, items_count, status)
           SELECT import_id, 'ZZ', 1, 'success' FROM import_subdivisions LIMIT 1"""
    )
    conn.commit()
    apply_023(conn)
    named = {r["subdivision_code"]: r["subdivision_name"] for r in rows(conn)}
    assert named["ZZ"] == "ZZ"
    assert named["980"] == "980"
    assert named["75"] == "Paris"
