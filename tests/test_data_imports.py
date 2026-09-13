"""The data_imports row a branch opens when it starts and resolves when it ends.

Runs on the throwaway database of conftest: the development database is not
an acceptable substrate, a crashed local pipeline leaves rows of its own there.
"""
import psycopg
import pytest

from src.pipeline import dag
from src.pipeline._db import last_import_comment, record_import, start_import
from src.pipeline.dag import record_failure


@pytest.fixture
def conn(migrated_conn, db_kwargs, monkeypatch):
    """The pipeline opens connections of its own: point them here too."""
    monkeypatch.setattr(dag, "connect", lambda: psycopg.connect(**db_kwargs))
    return migrated_conn


def _latest(conn, import_type):
    return conn.execute(
        "SELECT status, comment FROM data_imports WHERE type=%s"
        " ORDER BY created_at DESC LIMIT 1",
        (import_type,),
    ).fetchone()


def test_record_import_resolves_the_open_row_instead_of_stacking_one(conn):
    start_import(conn, "osm")
    assert _latest(conn, "osm") == ("pending", None)

    record_import(conn, "osm", None, "success", "v1")

    assert _latest(conn, "osm") == ("success", "v1")
    count = conn.execute("SELECT COUNT(*) FROM data_imports WHERE type='osm'").fetchone()
    assert count[0] == 1


def test_a_failing_step_closes_the_open_row_with_its_trace(conn):
    start_import(conn, "osm")

    record_failure("osm-whatever", RuntimeError("boom"))

    row = conn.execute(
        "SELECT status, comment FROM data_imports WHERE type='osm'"
    ).fetchall()
    assert len(row) == 1
    assert row[0][0] == "pending"
    assert "boom" in row[0][1]


def test_a_failed_run_leaves_the_guards_on_the_last_resolved_stamp(conn):
    """A rebuild swaps its object in at the end, so a failed one changed
    nothing: the tables are still the ones the last resolved row describes,
    and that is the comment the guards must keep reading — not the stack
    trace of the failure."""
    start_import(conn, "atp")
    record_import(conn, "atp", None, "success", "v1")

    start_import(conn, "atp")
    record_failure("atp-import", RuntimeError("boom"))

    assert last_import_comment(conn, "atp") == "v1"


def test_a_relaunch_supersedes_the_row_a_crashed_run_left_behind(conn):
    start_import(conn, "osm")  # crashes: never resolved

    start_import(conn, "osm")
    record_import(conn, "osm", None, "success")

    # Only the latest row counts, so the stale one needs no cleanup.
    assert _latest(conn, "osm")[0] == "success"


def test_an_unreachable_source_is_recorded_skipped_on_its_last_date(conn):
    """The displayed source date never goes backwards on an outage."""
    from datetime import datetime, timezone

    from src.pipeline.errors import SourceUnavailable

    when = datetime(2026, 8, 27, tzinfo=timezone.utc)
    record_import(conn, "osm", when, "success")
    start_import(conn, "osm")

    record_failure("osm-download", SourceUnavailable("Geofabrik"))

    date, status, comment = conn.execute(
        "SELECT date, status, comment FROM data_imports WHERE type='osm'"
        " ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    assert (date, status) == (when, "skipped")
    assert "Geofabrik" in comment


def test_a_shared_step_failing_lands_on_the_pipeline_row(conn):
    record_failure("mv-brand", RuntimeError("boom"))
    assert _latest(conn, "pipeline")[0] == "pending"


def test_recording_a_failure_never_raises(conn, monkeypatch):
    """Masking the original error would be worse."""
    monkeypatch.setattr(dag, "connect", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    record_failure("osm-import", RuntimeError("boom"))
