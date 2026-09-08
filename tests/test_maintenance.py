"""Runs on the throwaway database of conftest; skipped without PostGIS.

The development database is not an acceptable substrate: maintenance_since()
reads the latest row of *every* datasource, so a crashed local pipeline that
left 'osm' pending would fail these tests for reasons of its own.
"""
import psycopg
import pytest

from src.db import maintenance_since
from src.pipeline import dag
from src.pipeline._db import record_import, start_import
from src.pipeline.dag import record_failure


@pytest.fixture
def conn(migrated_conn, db_kwargs, monkeypatch):
    """The pipeline opens connections of its own: point them here too."""
    monkeypatch.setattr(dag, "connect", lambda: psycopg.connect(**db_kwargs))
    return migrated_conn


def test_pending_row_holds_maintenance_until_resolved(conn, db_kwargs):
    assert maintenance_since(conn) is None

    start_import(conn, "pipeline")
    started = maintenance_since(conn)
    assert started is not None

    # A new process (crashed run, restarted app) still sees the pending row.
    with psycopg.connect(**db_kwargs) as other:
        assert maintenance_since(other) == started

    # record_import resolves the pending row instead of stacking a new one.
    record_import(conn, "pipeline", None, "success")
    assert maintenance_since(conn) is None
    count = conn.execute("SELECT COUNT(*) FROM data_imports WHERE type='pipeline'").fetchone()
    assert count[0] == 1


def test_a_failing_step_closes_the_open_row_instead_of_stacking_one(conn):
    start_import(conn, "pipeline")

    record_failure("pipeline-whatever", RuntimeError("boom"))

    row = conn.execute(
        "SELECT status, comment FROM data_imports WHERE type='pipeline'"
    ).fetchall()
    assert len(row) == 1
    assert row[0][0] == "pending"  # still in maintenance
    assert "boom" in row[0][1]


def test_a_relaunch_supersedes_the_row_a_crashed_run_left_behind(conn):
    start_import(conn, "pipeline")  # crashes: never resolved
    assert maintenance_since(conn) is not None

    start_import(conn, "pipeline")
    record_import(conn, "pipeline", None, "success")

    # Only the latest row counts, so the stale one needs no cleanup.
    assert maintenance_since(conn) is None


def test_a_clean_run_lifts_the_maintenance_a_failed_one_left(conn):
    """The 'pipeline' row has no owner to supersede it.

    osm, atp and nsi each open a row when they start and close it when they
    end, so a later run resolves a stale one on its own. record_failure posts
    a 'pipeline' row for the steps that belong to no branch — mv-brand,
    cleanup — and nothing wrote one on success: a single failed run kept the
    site in maintenance for good, however many clean runs followed.
    """
    conn.execute(
        "INSERT INTO data_imports (type, date, status, comment)"
        " VALUES ('pipeline', NULL, 'pending', 'step ''mv-brand'' failed')"
    )
    conn.commit()
    assert maintenance_since(conn) is not None

    dag.record_success()

    assert maintenance_since(conn) is None
