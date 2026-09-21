"""Geofabrik outage must not fail the pipeline: we keep the data we have."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never, Self

import pytest
import requests

from src.pipeline import _matview, osm
from src.pipeline.constants import Region
from src.pipeline.errors import SourceUnavailableError


def region(**fields: Any) -> Region:  # noqa: ANN401 — the fields a test cares about
    """A Geofabrik region, only as complete as the test needs it."""
    return {"url": "", "state_url": "", "pbf_path": Path(), **fields}  # pyright: ignore[reportReturnType]


@pytest.fixture(autouse=True)
def _isolate_timestamp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osm, "GEOFABRIK_TS_PATH", tmp_path / "geofabrik-timestamp.txt")


def test_returns_none_when_every_region_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": region(), "belgium": region()})
    monkeypatch.setattr(osm, "geofabrik_timestamp", _boom)
    assert osm.newest_geofabrik_timestamp() is None


def test_uses_the_regions_that_did_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    ts = datetime(2026, 8, 27, tzinfo=UTC)
    regions = {"france": region(), "belgium": region()}
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", regions)

    def one_down(asked: Region) -> datetime:
        return _boom() if asked is regions["france"] else ts

    monkeypatch.setattr(osm, "geofabrik_timestamp", one_down)
    assert osm.newest_geofabrik_timestamp() == ts


def test_download_reports_the_source_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a pipeline failure: SourceUnavailableError, and the DB is never touched."""
    monkeypatch.setattr(osm, "newest_geofabrik_timestamp", lambda: None)
    monkeypatch.setattr(osm, "connect", _boom)  # no data_imports row opened
    try:
        osm.download_pbf()
    except SourceUnavailableError:
        pass
    else:
        raise AssertionError("expected SourceUnavailableError")


def _boom(*_args: object) -> Never:
    raise requests.ConnectionError("502 Bad Gateway")


def test_refuses_a_partial_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leftover PBF from a failed run must not become the whole planet."""
    present, absent = tmp_path / "belgium.pbf", tmp_path / "france.pbf"
    present.write_bytes(b"x")
    monkeypatch.setattr(
        osm,
        "GEOFABRIK_REGIONS",
        {
            "belgium": region(pbf_path=present),
            "france": region(pbf_path=absent),
        },
    )
    with pytest.raises(RuntimeError, match=r"france\.pbf"):
        osm.run_osm2pgsql()


def test_the_answer_is_reused_without_a_second_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """osm-probe, osm-download and osm-views must not each hit the network."""
    calls: list[Region] = []
    ts = datetime(2026, 8, 27, tzinfo=UTC)
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": region()})

    def answer(asked: Region) -> datetime:
        calls.append(asked)
        return ts

    monkeypatch.setattr(osm, "geofabrik_timestamp", answer)
    assert osm.newest_geofabrik_timestamp() == ts
    assert osm.newest_geofabrik_timestamp() == ts
    assert len(calls) == 1


def test_an_outage_is_remembered_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise the next step re-runs the whole retry sequence for nothing."""
    calls: list[Region] = []
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {"france": region()})

    def down(asked: Region) -> Never:
        calls.append(asked)
        _boom()

    monkeypatch.setattr(osm, "geofabrik_timestamp", down)
    assert osm.newest_geofabrik_timestamp() is None
    assert osm.newest_geofabrik_timestamp() is None
    assert len(calls) == 1


def test_forget_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    osm.GEOFABRIK_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
    osm.GEOFABRIK_TS_PATH.write_text("2020-01-01T00:00:00+00:00")
    osm.forget_geofabrik_timestamp()
    assert not osm.GEOFABRIK_TS_PATH.exists()


def test_the_subdivision_pieces_are_built_even_when_no_pbf_was_downloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pieces are derived from our code, not from Geofabrik's data.

    Production reported osm-import as done while the pieces had never been
    built: the step returns early when nothing was downloaded, and the call
    sat behind that return. The ATP import then failed on a missing table.
    """
    monkeypatch.setattr(
        osm, "GEOFABRIK_REGIONS", {"france": region(pbf_path=tmp_path / "absent.pbf")}
    )
    built: list[bool] = []
    monkeypatch.setattr(osm, "build_subdivision_parts", lambda: built.append(True))

    osm.run_osm2pgsql()

    assert built == [True]


def test_a_missing_subdivisions_table_fails_on_the_branch_that_owns_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An assertion, not a remedy: the download is gated on generic.lua's shape,
    so a deploy that starts writing subdivisions reimports on its own. Should it
    ever go missing, it fails here rather than in the ATP import three steps on.
    """
    monkeypatch.setattr(osm, "connect", _FakeConn)
    with pytest.raises(RuntimeError, match="No subdivisions table"):
        osm.build_subdivision_parts()


class _FakeCursor:
    """A cursor whose every query answers one NULL."""

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, *_a: object, **_kw: object) -> Self:
        return self

    def fetchone(self) -> tuple[None]:
        return (None,)


class _FakeConn:
    def cursor(self, *_a: object, **_kw: object) -> _FakeCursor:
        return _FakeCursor()

    def close(self) -> None:
        pass


def _download_pbf_with(
    monkeypatch: pytest.MonkeyPatch, *, tables_written_by_this_revision: bool
) -> list[str]:
    """Run download_pbf against a source that published nothing new."""
    ts = datetime(2026, 8, 27, tzinfo=UTC)
    recorded: list[str] = []

    def record(_conn: object, _kind: str, _date: object, status: str, *_a: object) -> None:
        recorded.append(status)

    def is_current(*_a: object) -> bool:
        return tables_written_by_this_revision

    monkeypatch.setattr(osm, "newest_geofabrik_timestamp", lambda: ts)
    monkeypatch.setattr(osm, "connect", _FakeConn)

    def last_date(_conn: object, _kind: str) -> datetime:
        return ts

    def no_start(_conn: object, _kind: str) -> None:
        return None

    monkeypatch.setattr(osm, "last_import_date", last_date)
    monkeypatch.setattr(osm, "start_import", no_start)
    monkeypatch.setattr(osm, "record_import", record)
    monkeypatch.setattr(_matview, "is_current", is_current)
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {})
    osm.download_pbf()
    return recorded


def test_unchanged_data_and_unchanged_revision_skip_the_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _download_pbf_with(monkeypatch, tables_written_by_this_revision=True) == ["skipped"]


def test_a_new_revision_reimports_without_waiting_for_geofabrik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline accounts for its own version, not only for the source's.

    Editing generic.lua leaves the Geofabrik timestamp untouched, so the date
    check alone would hold the new tables back until the upstream happens to
    publish — which is how production ran code expecting a subdivisions table
    against a database that had none.
    """
    assert _download_pbf_with(monkeypatch, tables_written_by_this_revision=False) == []


# --- Reading the timestamp of a region -------------------------------------------


class _Answer:
    def __init__(self, text: str = "", headers: dict[str, str] | None = None) -> None:
        self.text = text
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        pass


class _Http:
    """Geofabrik as staged: state.txt text, or the PBF's Last-Modified."""

    def __init__(self, state: str | None = None, last_modified: str | None = None) -> None:
        self.state, self.last_modified = state, last_modified

    def get(self, _url: str, timeout: float | None = None) -> _Answer:
        if self.state is None:
            raise requests.HTTPError("404")
        return _Answer(text=self.state)

    def head(
        self, _url: str, timeout: float | None = None, allow_redirects: bool | None = None
    ) -> _Answer:
        headers = {"Last-Modified": self.last_modified} if self.last_modified else {}
        return _Answer(headers=headers)


REGION = region(
    state_url="https://geofabrik.example/state.txt",
    url="https://geofabrik.example/x.pbf",
)


def test_the_state_file_is_read_with_its_escaped_colons(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        osm,
        "_session",
        _Http(
            state="# original OSM minutely replication sequence number 6543210\n"
            "sequenceNumber=4321\ntimestamp=2026-08-27T20\\:21\\:02Z\n"
        ),
    )
    assert osm.geofabrik_timestamp(REGION) == datetime(2026, 8, 27, 20, 21, 2, tzinfo=UTC)


def test_a_region_without_a_state_file_dates_its_pbf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(osm, "_session", _Http(last_modified="Thu, 27 Aug 2026 20:21:02 GMT"))
    assert osm.geofabrik_timestamp(REGION) == datetime(2026, 8, 27, 20, 21, 2, tzinfo=UTC)


def test_a_state_file_without_a_timestamp_falls_back_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        osm,
        "_session",
        _Http(state="sequenceNumber=1\n", last_modified="Thu, 27 Aug 2026 20:21:02 GMT"),
    )
    assert osm.geofabrik_timestamp(REGION).day == 27


def test_no_date_at_all_is_an_error_the_probe_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not a date: the region counts as down, and download_pbf decides."""
    monkeypatch.setattr(osm, "_session", _Http())
    with pytest.raises(ValueError, match="Cannot determine"):
        osm.geofabrik_timestamp(REGION)
