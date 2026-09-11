"""Swapping a rebuilt object in beside the live one.

Runs on the throwaway database of conftest, in its public schema: the helpers
retire and dispose of objects by name there, the way the pipeline does.
"""

import psycopg
import pytest

from src.pipeline import _matview

INDEXES = {"t_a_idx": "(a)"}


@pytest.fixture
def conn(db_kwargs):
    with psycopg.connect(**db_kwargs) as c:
        yield c
        c.rollback()
        with c.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS s CASCADE")
            cur.execute("DROP MATERIALIZED VIEW IF EXISTS v")
            cur.execute(
                """SELECT relname FROM pg_class
                    WHERE relnamespace = 'public'::regnamespace
                      AND relkind = 'r' AND starts_with(relname, 't')"""
            )
            for (name,) in cur.fetchall():
                cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        c.commit()


def _table(cur, name, value):
    cur.execute(f"CREATE TABLE {name} (a int)")
    cur.execute(f"INSERT INTO {name} VALUES ({value})")


def _value(conn, name):
    return conn.execute(f"SELECT a FROM {name}").fetchone()[0]


def _relations(conn, prefix):
    return {
        r[0]
        for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
            " AND relkind IN ('r', 'm', 'i') AND starts_with(relname, %s)",
            (prefix,),
        )
    }


def test_the_new_object_takes_the_name_and_the_index_names(conn):
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        cur.execute("CREATE INDEX t_a_idx ON t (a)")
        _table(cur, "t_new", 2)
        _matview.create_indexes(cur, "t_new", INDEXES)
        assert "t_a_idx_new" in _relations(conn, "t_a_idx")

        _matview.swap(cur, "TABLE", "t", "t_new", INDEXES)

    assert _value(conn, "t") == 2
    assert _value(conn, "t_old") == 1
    # The old index went with the old table, the new one took its name.
    assert _relations(conn, "t_a_idx") == {"t_a_idx"}
    assert conn.execute(
        "SELECT tablename FROM pg_indexes WHERE indexname = 't_a_idx'"
    ).fetchone()[0] == "t"


def test_the_stamp_travels_with_the_rename(conn):
    with conn.cursor() as cur:
        _table(cur, "t", 1)
        _table(cur, "t_new", 2)
        _matview.stamp(cur, "t_new", "sig", "TABLE")
        _matview.swap(cur, "TABLE", "t", "t_new")
    assert _matview.is_current(conn, "t", "sig")


def test_a_view_materialized_on_the_old_object_keeps_working(conn):
    """The reason the old object retires instead of being dropped: mv_places
    is materialized on points, mv_places_brand on mv_places. Each keeps its
    rows and its dependency, by OID, until its own swap."""
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


def test_a_retired_object_a_previous_run_left_is_moved_aside(conn):
    """mv-brand failed: the brand view still hangs on mv_places_old when
    osm-views retires the next mv_places. The leftover keeps its reader alive
    under a unique name, and both go with the next disposal."""
    with conn.cursor() as cur:
        _table(cur, "t_old", 0)
        cur.execute("CREATE MATERIALIZED VIEW v AS SELECT a FROM t_old")
        _table(cur, "t", 1)
        _table(cur, "t_new", 2)
        _matview.swap(cur, "TABLE", "t", "t_new")

        retired = _relations(conn, "t_old")
        assert len(retired) == 2 and "t_old" in retired
        assert _value(conn, "v") == 0
        assert _value(conn, "t_old") == 1

        cur.execute("DROP MATERIALIZED VIEW v")
        dropped = _matview.drop_retired(cur, "TABLE", "t")
    assert set(dropped) == retired
    assert _relations(conn, "t_old") == set()


def test_a_table_delivered_in_another_schema_moves_in(conn):
    """How osm2pgsql delivers: its tables, indexes included, in a schema of
    their own. Their index names are free in public once the old table's went."""
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


def test_a_first_build_has_nothing_to_retire(conn):
    with conn.cursor() as cur:
        _table(cur, "t_new", 2)
        _matview.create_indexes(cur, "t_new", INDEXES)
        _matview.swap(cur, "TABLE", "t", "t_new", INDEXES)
    assert _value(conn, "t") == 2
    assert _relations(conn, "t_old") == set()
    assert _relations(conn, "t_a_idx") == {"t_a_idx"}
