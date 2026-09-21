"""Swapping a rebuilt object in beside the live one.

Runs on the throwaway database of conftest, in its public schema: the helpers
retire and dispose of objects by name there, the way the pipeline does.
"""

from collections.abc import Iterator
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import sql

from src.config import Database
from src.pipeline import _matview
from tests.conftest import Connection, one

INDEXES: dict[str, LiteralString] = {"t_a_idx": "(a)"}
INDEX_NAMES = tuple(INDEXES)


@pytest.fixture
def conn(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as c:
        yield c
        c.rollback()
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS s CASCADE")
            cur.execute("DROP MATERIALIZED VIEW IF EXISTS v")
            # `t`, `t_new`, `t_old`, `t_old_<oid>` — and not every table
            # of the schema whose name starts with a t.
            cur.execute(
                """SELECT relname FROM pg_class
                    WHERE relnamespace = 'public'::regnamespace
                      AND relkind = 'r' AND (relname = 't' OR starts_with(relname, 't_'))"""
            )
            for (name,) in cur.fetchall():
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(name)))
        c.commit()


def _ident(name: str) -> sql.Identifier:
    """`schema.table` or `table`, as an identifier."""
    return sql.Identifier(*name.split("."))


def _table(cur: psycopg.Cursor[Any], name: str, value: int) -> None:
    cur.execute(sql.SQL("CREATE TABLE {} (a int)").format(_ident(name)))
    cur.execute(sql.SQL("INSERT INTO {} VALUES (%s)").format(_ident(name)), (value,))


def _value(conn: Connection, name: str) -> int:
    return one(conn.execute(sql.SQL("SELECT a FROM {}").format(_ident(name))).fetchone())[0]


def _relations(conn: Connection, prefix: str) -> set[str]:
    return {
        str(r[0])
        for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
            " AND relkind IN ('r', 'm', 'i') AND starts_with(relname, %s)",
            (prefix,),
        )
    }


def test_the_new_object_takes_the_name_and_the_index_names(conn: Connection) -> None:
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        cur.execute("CREATE INDEX t_a_idx ON t (a)")
        _table(cur, "t_new", 2)
        _matview.create_indexes(cur, "t_new", INDEXES)
        assert "t_a_idx_new" in _relations(conn, "t_a_idx")

        _matview.swap(cur, "TABLE", "t", "t_new", INDEX_NAMES)

    assert _value(conn, "t") == 2
    assert _value(conn, "t_old") == 1
    # The old index went with the old table, the new one took its name.
    assert _relations(conn, "t_a_idx") == {"t_a_idx"}
    indexed = one(
        conn.execute("SELECT tablename FROM pg_indexes WHERE indexname = 't_a_idx'").fetchone()
    )
    assert indexed[0] == "t"


def test_the_stamp_travels_with_the_rename(conn: Connection) -> None:
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        _table(cur, "t_new", 2)
        _matview.stamp(cur, "t_new", "sig", "TABLE")
        _matview.swap(cur, "TABLE", "t", "t_new")
    assert _matview.is_current(conn, "t", "sig")


def test_a_view_materialized_on_the_old_object_keeps_working(conn: Connection) -> None:
    """The reason the old object retires instead of being dropped: mv_places
    is materialized on points, mv_places_brand on mv_places. Each keeps its
    rows and its dependency, by OID, until its own swap.
    """
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        cur.execute("CREATE MATERIALIZED VIEW v AS SELECT a FROM t")
        _table(cur, "t_new", 2)
        _matview.swap(cur, "TABLE", "t", "t_new")

        assert _value(conn, "v") == 1
        # Still read by v: kept.
        assert _matview.drop_retired(cur, "TABLE", "t") == []
        assert "t_old" in _relations(conn, "t_old")

        cur.execute("DROP MATERIALIZED VIEW v")
        assert _matview.drop_retired(cur, "TABLE", "t") == ["t_old"]
    assert _relations(conn, "t_old") == set()


def test_a_retired_object_a_previous_run_left_is_moved_aside(conn: Connection) -> None:
    """mv-brand failed: the brand view still hangs on mv_places_old when
    osm-views retires the next mv_places. The leftover keeps its reader alive
    under a unique name, and both go with the next disposal.
    """
    with conn.cursor() as cur:
        _table(cur, "t_old", 0)
        cur.execute("CREATE MATERIALIZED VIEW v AS SELECT a FROM t_old")
        _table(cur, "t", 1)
        _table(cur, "t_new", 2)
        _matview.swap(cur, "TABLE", "t", "t_new")

        retired = _relations(conn, "t_old")
        assert len(retired) == 2
        assert "t_old" in retired
        assert _value(conn, "v") == 0
        assert _value(conn, "t_old") == 1

        cur.execute("DROP MATERIALIZED VIEW v")
        dropped = _matview.drop_retired(cur, "TABLE", "t")
    assert set(dropped) == retired
    assert _relations(conn, "t_old") == set()


def test_a_table_delivered_in_another_schema_moves_in(conn: Connection) -> None:
    """How osm2pgsql delivers: its tables, indexes included, in a schema of
    their own. Their index names are free in public once the old table's went.
    """
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        cur.execute("CREATE INDEX t_a_idx ON t (a)")
        cur.execute("CREATE SCHEMA s")
        _table(cur, "s.t", 2)
        cur.execute("CREATE INDEX t_a_idx ON s.t (a)")

        _matview.swap(cur, "TABLE", "t", "s.t")

    assert _value(conn, "t") == 2
    assert _value(conn, "t_old") == 1
    assert conn.execute(
        "SELECT schemaname, tablename FROM pg_indexes WHERE indexname = 't_a_idx'"
    ).fetchall() == [("public", "t")]
    assert conn.execute("SELECT to_regclass('s.t')").fetchone() == (None,)


def test_a_first_build_has_nothing_to_retire(conn: Connection) -> None:
    with conn.cursor() as cur:
        _table(cur, "t_new", 2)
        _matview.create_indexes(cur, "t_new", INDEXES)
        _matview.swap(cur, "TABLE", "t", "t_new", INDEX_NAMES)
    assert _value(conn, "t") == 2
    assert _relations(conn, "t_old") == set()
    assert _relations(conn, "t_a_idx") == {"t_a_idx"}
