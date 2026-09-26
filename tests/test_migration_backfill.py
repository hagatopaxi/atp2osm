"""End-to-end backfill of the existing history.

Needs the local PostGIS (podman-compose up -d); skipped otherwise.
Everything happens in a throwaway schema, on logs fabricated in tmp_path.
"""

import datetime
import json
import pathlib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql

from src.config import Database
from src.db import sql_file
from src.migrate import discover_migrations
from tests.conftest import Connection, load_module, one

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
SCHEMA: LiteralString = "test_backfill"

# A change as the 2025 logs hold it.
LoggedChange = dict[str, Any]


def load_migration(logs_dir: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The module reads ATP2OSM_LOGS_DIR at import time."""
    monkeypatch.setenv("ATP2OSM_LOGS_DIR", str(logs_dir))
    return load_module(MIGRATIONS / "016_backfill_import_departements.py")


def write_log(
    logs_dir: Path,
    brand: str,
    date: str,
    changes: list[LoggedChange],
    succeeded: list[int] | None = None,
) -> None:
    path = logs_dir / brand
    path.mkdir(parents=True, exist_ok=True)
    body = json.dumps(changes)
    if succeeded is not None:
        body += json.dumps(succeeded)
    (path / f"{date}.json").write_text(body)


def poi(
    dpt: int | str, changeset: int | None = None, brand: str = "Chez Michel", **tags: str
) -> LoggedChange:
    change: LoggedChange = {
        "departement_number": dpt,
        "atp_brand": brand,
        "tag": {"phone": "+33 1 23 45 67 89", **tags},
        "old_tag": {},
    }
    if changeset is not None:
        change["changeset"] = changeset
    return change


@pytest.fixture
def conn(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"SET search_path TO {SCHEMA}")
        # the whole history up to the child table, backfill excluded
        for version, path in sorted(_sql_migrations()):
            if version > 15:
                break
            c.execute(sql_file(path))
        c.commit()
        yield c
        c.rollback()  # a test can leave the transaction in a failed state
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.commit()


def _sql_migrations() -> list[tuple[int, Path]]:
    return [(v, p) for v, p in discover_migrations() if p.suffix == ".sql"]


def insert_import(conn: Connection, **kwargs: object) -> int:
    kwargs.setdefault("brand_wikidata", "Q1")
    kwargs.setdefault("brand_name", "Chez Michel")
    kwargs.setdefault("osm_user_id", 42)
    kwargs.setdefault("status", "success")
    row = conn.execute(
        sql.SQL("INSERT INTO import_history ({}) VALUES ({}) RETURNING id").format(
            sql.SQL(", ").join(map(sql.Identifier, kwargs)),
            sql.SQL(", ").join(sql.Placeholder() for _ in kwargs),
        ),
        list(kwargs.values()),
    ).fetchone()
    return int(one(row)[0])


def children(conn: Connection, import_id: int) -> list[tuple[Any, ...]]:
    return conn.execute(
        """SELECT departement_number, items_count, osm_changeset_id, status
           FROM import_departements WHERE import_id = %s
           ORDER BY departement_number""",
        (import_id,),
    ).fetchall()


DAY = datetime.datetime(2026, 5, 9, 14, 30, tzinfo=datetime.UTC)


def test_success_import_gets_one_row_per_departement(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(
        tmp_path,
        "Q1",
        "2026-05-09",
        [poi(69, 100), poi(69, 100), poi(1, 101)],
        succeeded=[100, 101],
    )
    import_id = insert_import(conn, import_date=DAY, changeset_ids=[100, 101])

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, import_id) == [
        ("01", 1, 101, "success"),
        ("69", 2, 100, "success"),
    ]


def test_fills_items_count_and_tags_count(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(
        tmp_path,
        "Q1",
        "2026-05-09",
        [poi(69, 100), poi(1, 101, website="https://x.example")],
        succeeded=[100, 101],
    )
    import_id = insert_import(conn, import_date=DAY)

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    row = one(
        conn.execute(
            "SELECT items_count, tags_count FROM import_history WHERE id = %s", (import_id,)
        ).fetchone()
    )
    assert row[0] == 2
    assert row[1] == {"phone": 2, "website": 1}


def test_partial_import_marks_the_departement_named_in_the_comment(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(
        tmp_path,
        "Q1",
        "2026-05-09",
        [poi(69, 100), poi(1, 101)],
        succeeded=[100],
    )
    import_id = insert_import(
        conn,
        import_date=DAY,
        status="partial_osm_api",
        comment="OSM API error for dept 1: HTTP 409 — conflict",
    )

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    # the failed département's changeset had been created: keep it
    assert children(conn, import_id) == [
        ("01", 1, 101, "error_osm_api"),
        ("69", 1, 100, "success"),
    ]
    # items_count only counts what actually went out
    assert (
        one(
            conn.execute(
                "SELECT items_count FROM import_history WHERE id = %s", (import_id,)
            ).fetchone()
        )[0]
        == 1
    )


def test_failed_import_has_no_successful_child(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "Q1", "2026-05-09", [poi(69), poi(1)], succeeded=[])
    import_id = insert_import(conn, import_date=DAY, status="error_unknown")

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert [c[3] for c in children(conn, import_id)] == [
        "error_unknown",
        "error_unknown",
    ]


def test_cancelled_and_empty_imports_are_left_alone(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "Q1", "2026-05-09", [poi(69, 100)], succeeded=[100])
    cancelled = insert_import(conn, import_date=DAY, status="cancelled")
    empty = insert_import(conn, import_date=DAY, status="success", items_count=0)

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, cancelled) == []
    assert children(conn, empty) == []


def test_import_without_log_keeps_its_changeset_ids_in_the_comment(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_id = insert_import(
        conn, import_date=DAY, comment="Rien à signaler", changeset_ids=[100, 101]
    )

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, import_id) == []
    assert (
        one(
            conn.execute(
                "SELECT comment FROM import_history WHERE id = %s", (import_id,)
            ).fetchone()
        )[0]
        == "Rien à signaler — Changesets : 100, 101"
    )


def test_two_imports_the_same_day_share_one_log_file(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "Q1", "2026-05-09", [poi(69, 100)], succeeded=[100])
    first = insert_import(conn, import_date=DAY, changeset_ids=[99])
    second = insert_import(conn, import_date=DAY + datetime.timedelta(hours=1), changeset_ids=[100])

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    # the file describes the last integration of the day
    assert children(conn, second) == [("69", 1, 100, "success")]
    assert children(conn, first) == []


def test_log_stored_under_the_osm_wikidata_is_found_back_by_brand_name(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the folder carries the OSM brand:wikidata of the first POI (Q246), the
    # integrated brand is the ATP one (Q699709)
    write_log(tmp_path, "Q246", "2026-05-09", [poi(69, 100)], succeeded=[100])
    import_id = insert_import(conn, import_date=DAY, brand_wikidata="Q699709")

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, import_id) == [("69", 1, 100, "success")]


def test_log_stored_under_unknown_is_found_back_by_brand_name(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "unknown", "2026-05-09", [poi(69, 100)], succeeded=[100])
    import_id = insert_import(conn, import_date=DAY)

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, import_id) == [("69", 1, 100, "success")]


def test_log_written_the_day_before_is_still_found(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # file named in local time, import_date in UTC
    write_log(tmp_path, "Q1", "2026-05-08", [poi(69, 100)], succeeded=[100])
    import_id = insert_import(
        conn, import_date=datetime.datetime(2026, 5, 9, 0, 30, tzinfo=datetime.UTC)
    )

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert children(conn, import_id) == [("69", 1, 100, "success")]


def test_running_twice_does_not_duplicate(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "Q1", "2026-05-09", [poi(69, 100)], succeeded=[100])
    import_id = insert_import(conn, import_date=DAY)
    module = load_migration(tmp_path, monkeypatch)

    module.BackfillImportDepartements(conn).migrate()
    module.BackfillImportDepartements(conn).migrate()

    assert len(children(conn, import_id)) == 1


def test_changeset_ids_is_dropped_by_the_cleanup(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(tmp_path, "Q1", "2026-05-09", [poi(69, 100)], succeeded=[100])
    insert_import(conn, import_date=DAY, changeset_ids=[100])

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()
    conn.execute(sql_file(dict(_sql_migrations())[17]))

    assert (
        conn.execute(
            """SELECT 1 FROM information_schema.columns
           WHERE table_schema = %s AND table_name = 'import_history'
             AND column_name = 'changeset_ids'""",
            (SCHEMA,),
        ).fetchone()
        is None
    )


def test_error_kind_comes_from_the_comment_department_by_department(
    conn: Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_log(
        tmp_path,
        "Q1",
        "2026-05-09",
        [poi(69, 100), poi(1, 101), poi(75, 102)],
        succeeded=[100],
    )
    import_id = insert_import(
        conn,
        import_date=DAY,
        status="partial_osm_api",  # le commentaire prime sur ce suffixe
        comment=("OSM API error for dept 1: HTTP 409 — conflict; Unknown error for dept 75: boom"),
    )

    load_migration(tmp_path, monkeypatch).BackfillImportDepartements(conn).migrate()

    assert [(d[0], d[3]) for d in children(conn, import_id)] == [
        ("01", "error_osm_api"),
        ("69", "success"),
        ("75", "error_unknown"),
    ]


def test_cleanup_flattens_legacy_statuses(conn: Connection) -> None:
    legacy = {
        insert_import(conn, import_date=DAY, status=s): s
        for s in (
            "success",
            "partial_osm_api",
            "partial_unknown",
            "cancelled",
            "error_osm_api",
            "error_unknown",
        )
    }

    conn.execute(sql_file(dict(_sql_migrations())[17]))

    got = {
        i: one(conn.execute("SELECT status FROM import_history WHERE id = %s", (i,)).fetchone())[0]
        for i in legacy
    }
    assert sorted(got.values()) == [
        "cancelled",
        "error",
        "error",
        "partial",
        "partial",
        "success",
    ]

    # la nouvelle contrainte accepte les valeurs plates et rien d'autre
    insert_import(conn, import_date=DAY, status="partial")
    with pytest.raises(psycopg.errors.CheckViolation):
        insert_import(conn, import_date=DAY, status="partial_osm_api")
