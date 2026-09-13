"""Geofabrik outage must not fail the pipeline: we keep the data we have."""
from datetime import datetime, timezone

import pytest

from src.pipeline import osm
from src.pipeline.errors import SourceUnavailable


@pytest.fixture(autouse=True)
def _isolate_timestamp_file(tmp_path, monkeypatch):
    monkeypatch.setattr(osm, "GEOFABRIK_TS_PATH", tmp_path / "geofabrik-timestamp.txt")


def test_returns_none_when_every_region_is_down(monkeypatch):
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": {}, "belgium": {}})
    monkeypatch.setattr(osm, "_geofabrik_timestamp", _boom)
    assert osm._newest_geofabrik_timestamp() is None


def test_uses_the_regions_that_did_answer(monkeypatch):
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": {}, "belgium": {}})
    monkeypatch.setattr(
        osm, "_geofabrik_timestamp",
        lambda region: _boom(region) if region is osm.GEOFABRIK_REGIONS["france"] else ts,
    )
    assert osm._newest_geofabrik_timestamp() == ts


def test_download_reports_the_source_as_unavailable(monkeypatch):
    """Not a pipeline failure: SourceUnavailable, and the DB is never touched."""
    monkeypatch.setattr(osm, "_newest_geofabrik_timestamp", lambda: None)
    monkeypatch.setattr(osm, "connect", _boom)  # no data_imports row opened
    try:
        osm.download_pbf()
    except SourceUnavailable:
        pass
    else:
        raise AssertionError("expected SourceUnavailable")


def _boom(*_args):
    raise RuntimeError("502 Bad Gateway")


def test_refuses_a_partial_import(tmp_path, monkeypatch):
    """A leftover PBF from a failed run must not become the whole planet."""
    present, absent = tmp_path / "belgium.pbf", tmp_path / "france.pbf"
    present.write_bytes(b"x")
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {
        "belgium": {"pbf_path": present}, "france": {"pbf_path": absent},
    })
    try:
        osm.run_osm2pgsql()
    except RuntimeError as exc:
        assert "france.pbf" in str(exc)
    else:
        raise AssertionError("imported a partial region set")


def test_the_answer_is_reused_without_a_second_round_trip(monkeypatch):
    """osm-probe, osm-download and osm-views must not each hit the network."""
    calls = []
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": {}})
    monkeypatch.setattr(osm, "_geofabrik_timestamp",
                        lambda region: (calls.append(region), ts)[1])
    assert osm._newest_geofabrik_timestamp() == ts
    assert osm._newest_geofabrik_timestamp() == ts
    assert len(calls) == 1


def test_an_outage_is_remembered_too(monkeypatch):
    """Otherwise the next step re-runs the whole retry sequence for nothing."""
    calls = []
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": {}})
    monkeypatch.setattr(osm, "_geofabrik_timestamp",
                        lambda region: (calls.append(region), _boom())[1])
    assert osm._newest_geofabrik_timestamp() is None
    assert osm._newest_geofabrik_timestamp() is None
    assert len(calls) == 1


def test_forget_clears_it(monkeypatch):
    osm.GEOFABRIK_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
    osm.GEOFABRIK_TS_PATH.write_text("2020-01-01T00:00:00+00:00")
    osm.forget_geofabrik_timestamp()
    assert not osm.GEOFABRIK_TS_PATH.exists()


def test_the_subdivision_pieces_are_built_even_when_no_pbf_was_downloaded(
    tmp_path, monkeypatch
):
    """The pieces are derived from our code, not from Geofabrik's data.

    Production reported osm-import as done while the pieces had never been
    built: the step returns early when nothing was downloaded, and the call
    sat behind that return. The ATP import then failed on a missing table.
    """
    monkeypatch.setattr(
        osm, "GEOFABRIK_REGIONS", {"france": {"pbf_path": tmp_path / "absent.pbf"}}
    )
    built = []
    monkeypatch.setattr(osm, "_build_subdivision_parts", lambda: built.append(True))

    osm.run_osm2pgsql()

    assert built == [True]


def test_a_missing_subdivisions_table_fails_on_the_branch_that_owns_it(monkeypatch):
    """An assertion, not a remedy: the download is gated on generic.lua's shape,
    so a deploy that starts writing subdivisions reimports on its own. Should it
    ever go missing, it fails here rather than in the ATP import three steps on.
    """
    class _Cursor:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def execute(self, *a, **kw): return self
        def fetchone(self): return (None,)


    class _Conn:
        def cursor(self, *a, **kw): return _Cursor()
        def close(self): pass

    monkeypatch.setattr(osm, "connect", lambda: _Conn())
    with pytest.raises(RuntimeError, match="No subdivisions table"):
        osm._build_subdivision_parts()


class _FakeCursor:
    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def execute(self, *a, **kw): return self
    def fetchone(self): return (None,)


class _FakeConn:
    def cursor(self, *a, **kw): return _FakeCursor()
    def close(self): pass


def _download_pbf_with(monkeypatch, tables_written_by_this_revision):
    """Run download_pbf against a source that published nothing new."""
    ts = datetime(2026, 8, 27, tzinfo=timezone.utc)
    recorded = []
    monkeypatch.setattr(osm, "_newest_geofabrik_timestamp", lambda: ts)
    monkeypatch.setattr(osm, "connect", lambda: _FakeConn())
    monkeypatch.setattr(osm, "last_import_date", lambda conn, kind: ts)
    monkeypatch.setattr(osm, "start_import", lambda conn, kind: None)
    monkeypatch.setattr(osm, "record_import",
                        lambda conn, kind, date, status, *a: recorded.append(status))
    monkeypatch.setattr(osm._matview, "is_current",
                        lambda *a: tables_written_by_this_revision)
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {})
    osm.download_pbf()
    return recorded


def test_unchanged_data_and_unchanged_revision_skip_the_download(monkeypatch):
    assert _download_pbf_with(monkeypatch, True) == ["skipped"]


def test_a_new_revision_reimports_without_waiting_for_geofabrik(monkeypatch):
    """The pipeline accounts for its own version, not only for the source's.

    Editing generic.lua leaves the Geofabrik timestamp untouched, so the date
    check alone would hold the new tables back until the upstream happens to
    publish — which is how production ran code expecting a subdivisions table
    against a database that had none.
    """
    assert _download_pbf_with(monkeypatch, False) == []


# --- Reading the timestamp of a region -------------------------------------------


class _Http:
    """Geofabrik as staged: state.txt text, or the PBF's Last-Modified."""

    def __init__(self, state=None, last_modified=None):
        self.state, self.last_modified = state, last_modified

    def get(self, url, timeout=None):
        if self.state is None:
            raise RuntimeError("404")
        return type("R", (), {"text": self.state, "raise_for_status": lambda s: None})()

    def head(self, url, timeout=None, allow_redirects=None):
        headers = {"Last-Modified": self.last_modified} if self.last_modified else {}
        return type("R", (), {"headers": headers, "raise_for_status": lambda s: None})()


REGION = {"state_url": "https://geofabrik.example/state.txt", "url": "https://geofabrik.example/x.pbf"}


def test_the_state_file_is_read_with_its_escaped_colons(monkeypatch):
    monkeypatch.setattr(osm, "_session", _Http(
        state="# original OSM minutely replication sequence number 6543210\n"
              "sequenceNumber=4321\ntimestamp=2026-08-27T20\\:21\\:02Z\n"
    ))
    assert osm._geofabrik_timestamp(REGION) == datetime(2026, 8, 27, 20, 21, 2, tzinfo=timezone.utc)


def test_a_region_without_a_state_file_dates_its_pbf(monkeypatch):
    monkeypatch.setattr(osm, "_session", _Http(last_modified="Thu, 27 Aug 2026 20:21:02 GMT"))
    assert osm._geofabrik_timestamp(REGION) == datetime(2026, 8, 27, 20, 21, 2, tzinfo=timezone.utc)


def test_a_state_file_without_a_timestamp_falls_back_too(monkeypatch):
    monkeypatch.setattr(osm, "_session", _Http(state="sequenceNumber=1\n",
                                                last_modified="Thu, 27 Aug 2026 20:21:02 GMT"))
    assert osm._geofabrik_timestamp(REGION).day == 27


def test_no_date_at_all_is_an_error_the_probe_reports(monkeypatch):
    """Not a date: the region counts as down, and download_pbf decides."""
    monkeypatch.setattr(osm, "_session", _Http())
    with pytest.raises(ValueError):
        osm._geofabrik_timestamp(REGION)
