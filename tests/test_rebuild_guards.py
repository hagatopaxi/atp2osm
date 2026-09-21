"""The rebuild guards and the swaps of every pipeline branch, on the real
database.

Each step decides on two triggers — new data, new revision — and a step that
rebuilds never drops what the site reads: it builds beside the live object
and swaps in at the end, in the transaction that records the import. These
tests drive the real steps on the throwaway database with the network and
the external tools staged: the Geofabrik timestamp is handed over, osm2pgsql
is a function that writes the tables it would, DuckDB reads a parquet the
test built from three features.

Every test here has one of two shapes: "nothing moved, the step no-ops and
says so", or "something moved, the step rebuilds and the live object is
never missing in between".
"""

import json
import subprocess
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, LiteralString, Never

import psycopg
import pytest
import requests
from psycopg import sql

from src.config import Database
from src.db import code_sql
from src.phone import ensure_normalize_phone
from src.pipeline import _matview, _version, atp, atp2osm, nsi, osm
from src.pipeline._db import record_import, start_import
from src.pipeline.errors import SourceUnavailableError
from src.pipeline.ndgeojson_to_parquet import convert_to_parquet
from tests.conftest import Connection, one

# A staged HTTP answer, a staged download, what a test does to move an input.
Download = Callable[..., None]
Move = Callable[[Connection, pytest.MonkeyPatch], object]

TS = datetime(2026, 8, 27, tzinfo=UTC)
EARLIER = TS - timedelta(days=1)
LATER = TS + timedelta(days=1)

# The shape osm2pgsql leaves, reduced to what the views and the attachment read.
OSM_TABLES_SQL = """
    CREATE TABLE {schema}.points (
        node_id INT8 PRIMARY KEY, tags JSONB, geom GEOMETRY(Point, 4326) NOT NULL,
        version INT, osm_timestamp INT8
    );
    CREATE TABLE {schema}.polygons (
        area_id INT8 PRIMARY KEY, osm_type TEXT NOT NULL, tags JSONB, members JSONB,
        geom GEOMETRY(Geometry, 4326) NOT NULL, version INT, osm_timestamp INT8
    );
    CREATE TABLE {schema}.subdivisions (
        area_id INT8, osm_id INT8 NOT NULL, ref TEXT, name TEXT NOT NULL,
        admin_level INT NOT NULL, geom GEOMETRY(Geometry, 4326) NOT NULL
    );
    INSERT INTO {schema}.points VALUES
        (101, '{{"name": "Babylone", "shop": "clothes", "brand:wikidata": "Q1"}}',
         ST_SetSRID(ST_Point(2.35, 48.85), 4326), 3, 1700000000),
        (102, '{{"highway": "crossing"}}', ST_SetSRID(ST_Point(2.36, 48.86), 4326), 1, 1700000000);
    INSERT INTO {schema}.polygons VALUES
        (201, 'W', '{{"name": "Babylone", "shop": "clothes"}}', NULL,
         ST_MakeEnvelope(2.36, 48.86, 2.361, 48.861, 4326), 2, 1700000000);
    INSERT INTO {schema}.subdivisions VALUES
        (-1, 1, 'FR', 'France', 2, ST_MakeEnvelope(-5, 41, 10, 52, 4326)),
        (-2, 2, '75', 'Paris', 6, ST_MakeEnvelope(2.2, 48.8, 2.5, 48.9, 4326)),
        (-3, 3, 'MQ', 'Martinique', 2, ST_MakeEnvelope(-61.3, 14.3, -60.8, 14.9, 4326));
"""


@pytest.fixture
def pipeline(
    migrated_conn: Connection, test_db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Connection]:
    """The pipeline on the throwaway database, with the OSM tables of a
    previous import in place, and every file it writes under tmp_path.
    """
    # Two ways in: the psycopg connections, and the settings DuckDB and
    # osm2pgsql are handed to reach the same database.
    for module in (osm, atp, nsi):
        monkeypatch.setattr(module, "connect", lambda: psycopg.connect(test_db.conninfo))
    for module in (osm, atp):
        monkeypatch.setattr(module, "get_database", lambda: test_db)
    monkeypatch.setattr(osm, "GEOFABRIK_TS_PATH", tmp_path / "osm" / "geofabrik-timestamp.txt")
    monkeypatch.setattr(
        osm,
        "GEOFABRIK_REGIONS",
        {
            "france": {
                "url": "https://geofabrik.example/france-latest.osm.pbf",
                "state_url": "https://geofabrik.example/france-updates/state.txt",
                "pbf_path": tmp_path / "osm" / "france-latest.osm.pbf",
            },
        },
    )

    conn = migrated_conn
    ensure_normalize_phone(conn)
    conn.execute("DROP TABLE IF EXISTS atp_places, atp_spiders")
    conn.execute(code_sql(OSM_TABLES_SQL.format(schema="public")))
    conn.execute("""
        CREATE TABLE subdivision_parts AS
            SELECT osm_id, ref, name, admin_level, geom FROM subdivisions;
        CREATE INDEX ON subdivision_parts USING GIST (geom);
    """)
    conn.commit()
    yield conn

    # Everything a step may have built, retired or left behind. The stubs
    # `_migrated` created come back for the tests that follow.
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT c.relkind, c.relname FROM pg_class c
             WHERE c.relnamespace = 'public'::regnamespace
               AND c.relkind IN ('r', 'm')
               AND (c.relname ~ '^(mv_places|points|polygons|subdivisions|atp_places|atp_spiders)'
                    OR c.relname = 'subdivision_parts')
             ORDER BY c.relkind DESC  -- views first: they depend on the tables
        """)
        for kind, name in cur.fetchall():
            cur.execute(
                sql.SQL("DROP {} IF EXISTS {} CASCADE").format(
                    sql.SQL("MATERIALIZED VIEW" if kind == "m" else "TABLE"), sql.Identifier(name)
                )
            )
        cur.execute(f"DROP SCHEMA IF EXISTS {osm.IMPORT_SCHEMA} CASCADE")
        cur.execute("""
            CREATE TABLE atp_places (id TEXT, spider_id TEXT, brand_wikidata TEXT, brand TEXT);
            CREATE TABLE atp_spiders (spider TEXT, filename TEXT, errors INT8, features INT8,
                                      elapsed_time FLOAT8, updated_at TIMESTAMPTZ);
        """)
    conn.commit()


def imports(conn: Connection, kind: str) -> list[tuple[Any, ...]]:
    """(date, status, comment) of every row of that datasource, oldest first."""
    return conn.execute(
        "SELECT date, status, comment FROM data_imports WHERE type = %s ORDER BY id",
        (kind,),
    ).fetchall()


def oid(conn: Connection, name: str) -> int | None:
    # A name resolved inside an open transaction is the one that transaction
    # first saw: the step swapped on a connection of its own, so look afresh.
    conn.rollback()
    return one(conn.execute("SELECT to_regclass(%s)::oid", (name,)).fetchone())[0]


def count(conn: Connection, query: LiteralString) -> int:
    """The number a counting query answers."""
    return int(one(conn.execute(query).fetchone())[0])


def stamp(conn: Connection, name: str, sig: str, kind: LiteralString = "TABLE") -> None:
    with conn.cursor() as cur:
        _matview.stamp(cur, name, sig, kind)
    conn.commit()


def geofabrik(monkeypatch: pytest.MonkeyPatch, newest: datetime | None) -> None:
    monkeypatch.setattr(osm, "newest_geofabrik_timestamp", lambda: newest)


# =============================================================================
# osm-download
# =============================================================================


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Path]]:
    """What download_pbf fetched: (url, path) per call. Writes a file."""
    calls: list[tuple[str, Path]] = []

    def download(url: str, path: Path, session: object = None) -> None:
        calls.append((url, Path(path)))
        Path(path).write_bytes(b"pbf")

    monkeypatch.setattr(osm, "download_large_file", download)
    return calls


def test_nothing_new_and_the_same_revision_skips_the_download(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", _version.app_version())
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert downloads == []
    assert imports(pipeline, "osm")[-1] == (TS, "skipped", None)


def test_newer_data_is_downloaded_and_the_row_stays_open(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    """The row opened here is resolved by osm-views, once the tables are in."""
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", _version.app_version())
    geofabrik(monkeypatch, LATER)

    osm.download_pbf()

    assert [url for url, _ in downloads] == ["https://geofabrik.example/france-latest.osm.pbf"]
    assert osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()
    assert imports(pipeline, "osm")[-1][1] == "pending"


def test_a_new_revision_downloads_without_new_data(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", "a-previous-revision")
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert len(downloads) == 1


def test_tables_that_were_never_stamped_are_reimported(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    """A database from before the guards: no stamp is not the current stamp."""
    record_import(pipeline, "osm", TS, "success")
    geofabrik(monkeypatch, TS)
    osm.download_pbf()
    assert len(downloads) == 1


def test_a_first_run_downloads(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    geofabrik(monkeypatch, TS)
    osm.download_pbf()
    assert len(downloads) == 1


def test_a_pbf_already_on_disk_is_not_fetched_again(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    """A run that crashed after the download resumes from the file."""
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]
    pbf.parent.mkdir(parents=True)
    pbf.write_bytes(b"pbf")
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert downloads == []


def test_a_failed_download_leaves_no_partial_file(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]

    def download(_url: str, path: Path, session: object = None) -> Never:
        Path(path).write_bytes(b"half a pl")
        raise requests.ConnectionError("reset by peer")

    monkeypatch.setattr(osm, "download_large_file", download)
    geofabrik(monkeypatch, TS)

    with pytest.raises(requests.ConnectionError):
        osm.download_pbf()

    # Otherwise the next run would take the stump for the whole extract.
    assert not pbf.exists()


def test_geofabrik_down_opens_no_row(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch, downloads: list[tuple[str, Path]]
) -> None:
    geofabrik(monkeypatch, None)
    with pytest.raises(SourceUnavailableError):
        osm.download_pbf()
    assert imports(pipeline, "osm") == []
    assert downloads == []


# =============================================================================
# osm-import
# =============================================================================


class _Usage:
    """shutil.disk_usage's answer, the one field the check reads."""

    def __init__(self, free: int) -> None:
        self.free = free


def _plenty_of_disk(_path: Path) -> _Usage:
    return _Usage(free=10**12)


def _almost_full_disk(_path: Path) -> _Usage:
    return _Usage(free=10**9)


def _broken_view(name: str) -> str:
    return f"CREATE MATERIALIZED VIEW {name} AS SELECT 1/0"


# What the staged osm2pgsql recorded of its call.
Osm2pgsqlCall = dict[str, Any]


@pytest.fixture
def osm2pgsql(
    pipeline: Connection, test_db: Database, monkeypatch: pytest.MonkeyPatch
) -> list[Osm2pgsqlCall]:
    """osm2pgsql as a function: writes the tables into the import schema, the
    way the real one does with --create, and records how it was called.
    """
    calls: list[Osm2pgsqlCall] = []

    def run(args: list[str], check: bool, env: dict[str, str]) -> None:
        calls.append({"args": args, "env": env})
        schema = env["ATP2OSM_IMPORT_SCHEMA"]
        with psycopg.connect(test_db.conninfo) as c:
            c.execute(code_sql(OSM_TABLES_SQL.format(schema=schema)))
            # A marker telling the new table from the old one.
            c.execute(
                sql.SQL(
                    "INSERT INTO {}.points VALUES"
                    ' (999, \'{{"name": "fresh"}}\', ST_SetSRID(ST_Point(0, 0), 4326), 1, 1)'
                ).format(sql.Identifier(schema))
            )
            c.commit()

    monkeypatch.setattr(osm.subprocess, "run", run)
    monkeypatch.setattr(osm.shutil, "disk_usage", _plenty_of_disk)
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]
    pbf.parent.mkdir(parents=True, exist_ok=True)
    pbf.write_bytes(b"pbf")
    return calls


def test_the_import_swaps_the_new_tables_in_and_retires_the_old(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall]
) -> None:
    before = {t: oid(pipeline, t) for t in osm.OSM_TABLES}

    osm.run_osm2pgsql()

    (call,) = osm2pgsql
    assert call["env"]["ATP2OSM_IMPORT_SCHEMA"] == osm.IMPORT_SCHEMA
    assert call["env"]["ATP2OSM_ADMIN_LEVEL_MAX"] == str(osm.ADMIN_LEVEL_MAX)
    assert "PGPASSWORD" in call["env"]
    assert "-x" in call["args"]
    assert "flex" in call["args"]
    for table in osm.OSM_TABLES:
        assert oid(pipeline, table) != before[table], f"{table} was not swapped"
        assert oid(pipeline, f"{table}_old") == before[table], f"{table} was dropped, not retired"
    assert count(pipeline, "SELECT count(*) FROM points WHERE node_id = 999") == 1
    assert _matview.is_current(pipeline, "points", _version.app_version())
    assert oid(pipeline, f"{osm.IMPORT_SCHEMA}.points") is None
    assert not osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()
    # The pieces are cut from the new boundaries.
    assert _matview.is_current(
        pipeline,
        "subdivision_parts",
        _matview.signature(_version.app_version(), oid(pipeline, "subdivisions")),
    )


def test_a_view_on_the_old_tables_keeps_serving_through_the_swap(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall]
) -> None:
    pipeline.execute("CREATE MATERIALIZED VIEW mv_places AS SELECT node_id FROM points")
    pipeline.commit()

    osm.run_osm2pgsql()

    assert count(pipeline, "SELECT count(*) FROM mv_places") == 2


def test_a_failed_osm2pgsql_leaves_the_live_tables_and_the_pbf(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = {t: oid(pipeline, t) for t in osm.OSM_TABLES}

    def fail(_args: list[str], check: bool, env: dict[str, str]) -> Never:
        raise subprocess.CalledProcessError(1, "osm2pgsql")

    monkeypatch.setattr(osm.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        osm.run_osm2pgsql()

    for table in osm.OSM_TABLES:
        assert oid(pipeline, table) == before[table]
    # Retried from here, without downloading again.
    assert osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()


def test_a_full_disk_fails_before_osm2pgsql_starts(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(osm.shutil, "disk_usage", _almost_full_disk)
    with pytest.raises(RuntimeError, match="free disk"):
        osm.run_osm2pgsql()
    assert osm2pgsql == []


def test_the_pieces_are_recut_only_when_the_boundaries_moved(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall]
) -> None:
    osm.run_osm2pgsql()
    pieces = oid(pipeline, "subdivision_parts")

    osm.build_subdivision_parts()
    assert oid(pipeline, "subdivision_parts") == pieces

    osm.GEOFABRIK_REGIONS["france"]["pbf_path"].write_bytes(b"pbf")
    osm.run_osm2pgsql()
    assert oid(pipeline, "subdivision_parts") != pieces


def test_a_leftover_import_schema_is_started_over(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall]
) -> None:
    """A crashed run leaves its schema: --create would fail on the tables in it."""
    pipeline.execute(
        f"CREATE SCHEMA {osm.IMPORT_SCHEMA}; CREATE TABLE {osm.IMPORT_SCHEMA}.points (x INT)"
    )
    pipeline.commit()
    osm.run_osm2pgsql()
    assert count(pipeline, "SELECT count(*) FROM points WHERE node_id = 999") == 1


# =============================================================================
# osm-views
# =============================================================================


def nsi_imported(conn: Connection, version: str = "8.0.20260729") -> None:
    record_import(conn, "nsi", TS, "success", nsi.stamp(version))


def test_the_first_build_creates_the_view_and_resolves_the_row(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    nsi_imported(pipeline)
    start_import(pipeline, "osm")
    geofabrik(monkeypatch, TS)

    osm.setup_mv_places()

    assert imports(pipeline, "osm") == [(TS, "success", None)]
    rows = pipeline.execute("SELECT osm_id, node_type FROM mv_places ORDER BY osm_id").fetchall()
    # The crossing carries nothing a match can key on.
    assert rows == [(101, "node"), (201, "way")]
    indexes = {
        r[0]
        for r in pipeline.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'mv_places'"
        ).fetchall()
    }
    assert indexes == set(osm.MV_PLACES_INDEXES)


def test_unchanged_inputs_skip_the_rebuild(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") == built
    assert imports(pipeline, "osm")[-1][1] == "skipped"


def test_newer_osm_data_rebuilds(pipeline: Connection, monkeypatch: pytest.MonkeyPatch) -> None:
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built
    assert oid(pipeline, "mv_places_old") == built
    assert imports(pipeline, "osm")[-1] == (LATER, "success", None)


def test_a_new_nsi_release_rebuilds_without_new_osm_data(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view completes brand:wikidata from nsi_brands: it is an input."""
    nsi_imported(pipeline, "8.0.20260729")
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    nsi_imported(pipeline, "8.0.20260801")
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built


def test_a_new_revision_rebuilds_without_new_data(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    monkeypatch.setattr(osm, "app_version", lambda: "next-deploy")
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built


def test_geofabrik_down_still_rebuilds_on_the_other_inputs(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OSM data cannot have moved, the NSI release can have."""
    nsi_imported(pipeline, "8.0.20260729")
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    geofabrik(monkeypatch, None)
    osm.setup_mv_places()
    assert oid(pipeline, "mv_places") == built
    assert imports(pipeline, "osm")[-1][1] == "skipped"

    nsi_imported(pipeline, "8.0.20260801")
    osm.setup_mv_places()
    assert oid(pipeline, "mv_places") != built
    # The date recorded is the one already there: nothing new was seen.
    assert imports(pipeline, "osm")[-1] == (TS, "success", None)


def test_geofabrik_down_on_a_first_run_builds_nothing(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    geofabrik(monkeypatch, None)
    osm.setup_mv_places()
    assert oid(pipeline, "mv_places") is None
    assert imports(pipeline, "osm") == []


def test_a_failed_build_leaves_the_live_view_and_records_no_success(
    pipeline: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    geofabrik(monkeypatch, LATER)
    monkeypatch.setattr(osm, "mv_places_sql", _broken_view)
    with pytest.raises(psycopg.Error):
        osm.setup_mv_places()

    assert oid(pipeline, "mv_places") == built
    assert oid(pipeline, "mv_places_new") is None
    assert imports(pipeline, "osm")[-1] == (TS, "success", None)


def test_a_new_view_is_built_from_the_new_tables(
    pipeline: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After osm-import, osm-views reads points, not points_old."""
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    osm.run_osm2pgsql()

    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()

    assert count(pipeline, "SELECT count(*) FROM mv_places WHERE osm_id = 999") == 1


# =============================================================================
# atp-download
# =============================================================================


class _Json:
    def __init__(self, payload: Any) -> None:  # noqa: ANN401 — whatever the test staged
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:  # noqa: ANN401
        return self._payload


def run(run_id: str, end_time: datetime) -> atp.Run:
    return {
        "run_id": run_id,
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parquet_url": f"https://atp.example/{run_id}/output.parquet",
        "output_url": f"https://atp.example/{run_id}/output.zip",
        "stats_url": f"https://atp.example/{run_id}/stats.json",
    }


@pytest.fixture
def atp_workdir(pipeline: Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The ATP working directory, with the leftovers of a previous run."""
    workdir = tmp_path / "atp"
    workdir.mkdir()
    monkeypatch.setattr(atp, "ATP_DIR", workdir)
    monkeypatch.setattr(atp, "SPIDERS_PATH", workdir / "spiders.json")
    monkeypatch.setattr(atp, "PARQUET_PATH", workdir / "latest.parquet")
    monkeypatch.setattr(atp, "GEOJSON_DIR", workdir / "geojson")
    monkeypatch.setattr(atp, "SPLIT_DIR", workdir / "split")
    return workdir


@pytest.fixture
def atp_history(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """ATP's run history, oldest first as it is published, and the files
    download_atp fetched.
    """
    state: dict[str, Any] = {"runs": [], "fetched": []}

    def get(_url: str, timeout: float | None = None) -> _Json:
        if isinstance(state["runs"], Exception):
            raise state["runs"]
        return _Json(state["runs"])

    def download(url: str, path: Path) -> None:
        state["fetched"].append(url)
        if url.endswith("stats.json"):
            Path(path).write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "spider": "babylone_fr",
                                "filename": "locations/spiders/babylone_fr.py",
                                "errors": 0,
                                "features": 2,
                                "elapsed_time": 1.5,
                            },
                        ]
                    }
                )
            )
        else:
            Path(path).write_bytes(b"zip")

    monkeypatch.setattr(atp.requests, "get", get)
    monkeypatch.setattr(atp, "download_large_file", download)
    monkeypatch.setattr(
        atp,
        "spider_dates",
        lambda: {
            "locations/spiders/babylone_fr.py": "2026-08-01T00:00:00+00:00",
        },
    )
    return state


def test_nothing_new_from_atp_skips_and_keeps_the_stamp(
    pipeline: Connection, atp_workdir: Path, atp_history: dict[str, Any]
) -> None:
    """The row says 'skipped' with the comment already there: what describes
    the table in place is the revision that built it, not the one running.
    """
    record_import(pipeline, "atp", TS, "success", "the-revision-that-built-it")
    atp_history["runs"] = [run("r1", EARLIER), run("r2", TS)]
    (atp_workdir / "geojson").mkdir()
    (atp_workdir / "output.zip").write_bytes(b"old")
    (atp_workdir / "latest.parquet").write_bytes(b"parquet")

    atp.download_atp()

    assert atp_history["fetched"] == []
    assert imports(pipeline, "atp")[-1] == (TS, "skipped", "the-revision-that-built-it")
    # The leftovers of a crashed run would rebuild a parquet nothing needs.
    assert not (atp_workdir / "geojson").exists()
    assert not (atp_workdir / "output.zip").exists()
    assert (atp_workdir / "latest.parquet").exists()


def test_a_new_run_is_downloaded_with_its_dated_spiders(
    pipeline: Connection, atp_workdir: Path, atp_history: dict[str, Any]
) -> None:
    record_import(pipeline, "atp", TS, "success", "v1")
    atp_history["runs"] = [run("r2", TS), run("r3", LATER)]

    atp.download_atp()

    assert atp_history["fetched"] == [
        "https://atp.example/r3/output.zip",
        "https://atp.example/r3/stats.json",
    ]
    assert (atp_workdir / "output.zip").exists()
    (spider,) = json.loads((atp_workdir / "spiders.json").read_text())
    assert spider["updated_at"] == "2026-08-01T00:00:00+00:00"
    assert not (atp_workdir / "stats.json").exists()
    # Resolved by atp-import, once the table is in.
    assert imports(pipeline, "atp")[-1][1] == "pending"


def test_a_first_run_takes_the_newest(
    pipeline: Connection, atp_workdir: Path, atp_history: dict[str, Any]
) -> None:
    atp_history["runs"] = [run("r1", EARLIER), run("r2", TS)]
    atp.download_atp()
    assert atp_history["fetched"][0] == "https://atp.example/r2/output.zip"


def test_atp_unreachable_is_a_source_outage(
    pipeline: Connection, atp_workdir: Path, atp_history: dict[str, Any]
) -> None:
    atp_history["runs"] = requests.ConnectionError("dns")
    with pytest.raises(SourceUnavailableError):
        atp.download_atp()
    # The row is left open for the runner to resolve as skipped.
    assert imports(pipeline, "atp")[-1][1] == "pending"


def test_github_down_costs_the_dates_not_the_run(
    pipeline: Connection,
    atp_workdir: Path,
    atp_history: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    atp_history["runs"] = [run("r3", LATER)]

    def no_git() -> Never:
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(atp, "spider_dates", no_git)

    atp.download_atp()

    (spider,) = json.loads((atp_workdir / "spiders.json").read_text())
    assert spider["updated_at"] is None


# =============================================================================
# atp-import
# =============================================================================


def feature(
    feature_id: str, spider: str, country: str, lon: float | None, lat: float | None, **props: str
) -> dict[str, Any]:
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": {
            "@spider": spider,
            "addr:country": country,
            "brand": "Babylone",
            "brand:wikidata": "Q1",
            "name": f"Babylone {feature_id}",
            "email": "SHOP@Babylone.example",
            **props,
        },
        "geometry": None if lon is None else {"type": "Point", "coordinates": [lon, lat]},
    }


@pytest.fixture
def parquet(atp_workdir: Path) -> Path:
    """A parquet built by the pipeline's own converter, from features of
    three countries, one of them at sea, one of them without a location.
    """
    split = atp_workdir / "split"
    split.mkdir()
    features = [
        feature("paris", "babylone_fr", "FR", 2.35, 48.85, phone="+33 1 00 00 00 00"),
        feature("fort-de-france", "babylone_mq", "MQ", -61.07, 14.6),
        feature("berlin", "babylone_de", "DE", 13.4, 52.5),
        feature("atlantic", "babylone_fr", "FR", -30.0, 45.0),
        feature("nowhere", "babylone_fr", "FR", None, None),
    ]
    (split / "part.geojson").write_text("\n".join(json.dumps(f) for f in features) + "\n")
    convert_to_parquet(split, atp_workdir / "latest.parquet")
    (atp_workdir / "spiders.json").write_text(
        json.dumps(
            [
                {
                    "spider": "babylone_fr",
                    "filename": "locations/spiders/babylone_fr.py",
                    "errors": 0,
                    "features": 3,
                    "elapsed_time": 1.5,
                    "updated_at": "2026-08-01T00:00:00+00:00",
                },
                {
                    "spider": "babylone_mq",
                    "filename": "locations/spiders/babylone_mq.py",
                    "errors": 0,
                    "features": 1,
                    "elapsed_time": 0.5,
                    "updated_at": None,
                },
                {
                    "spider": "babylone_de",
                    "filename": "locations/spiders/babylone_de.py",
                    "errors": 0,
                    "features": 1,
                    "elapsed_time": 0.5,
                    "updated_at": None,
                },
            ]
        )
    )
    return atp_workdir / "latest.parquet"


def atp_rows(conn: Connection) -> list[tuple[Any, ...]]:
    conn.rollback()
    return conn.execute(
        "SELECT id, subdivision_code, subdivision_name, email FROM atp_places ORDER BY id"
    ).fetchall()


def test_the_import_keeps_the_country_and_attaches_every_poi(
    pipeline: Connection, parquet: Path
) -> None:
    start_import(pipeline, "atp")
    before = oid(pipeline, "atp_places")

    atp.import_atp()

    # Berlin is another country's, the Atlantic one is in no subdivision,
    # `nowhere` has no geometry; Martinique attaches at the country level.
    assert atp_rows(pipeline) == [
        ("fort-de-france", "MQ", "Martinique", "shop@babylone.example"),
        ("paris", "75", "Paris", "shop@babylone.example"),
    ]
    assert oid(pipeline, "atp_places_old") == before
    indexes = {
        r[0]
        for r in pipeline.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'atp_places'"
        ).fetchall()
    }
    assert indexes == set(atp.ATP_PLACES_INDEXES)
    spiders = pipeline.execute(
        "SELECT spider, updated_at FROM atp_spiders ORDER BY spider"
    ).fetchall()
    assert [s[0] for s in spiders] == ["babylone_fr", "babylone_mq"]
    assert spiders[0][1] is not None
    (recorded,) = imports(pipeline, "atp")
    assert recorded[1:] == ("success", _version.app_version())
    assert recorded[0] == datetime.fromtimestamp(parquet.stat().st_mtime, tz=UTC)


def test_the_same_parquet_by_the_same_revision_is_not_imported_twice(
    pipeline: Connection, parquet: Path
) -> None:
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    atp.import_atp()

    assert oid(pipeline, "atp_places") == built
    assert len(imports(pipeline, "atp")) == 1


def test_a_new_revision_reimports_the_same_parquet(
    pipeline: Connection, parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    atp.import_atp()

    assert oid(pipeline, "atp_places") != built
    assert imports(pipeline, "atp")[-1][2] == "next-deploy"


def test_a_newer_parquet_is_imported(pipeline: Connection, parquet: Path) -> None:
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    import os

    later = parquet.stat().st_mtime + 3600
    os.utime(parquet, (later, later))
    atp.import_atp()

    assert oid(pipeline, "atp_places") != built


def test_a_skipped_download_is_not_re_imported(pipeline: Connection, parquet: Path) -> None:
    """download_atp found nothing new and stamped the row with the revision
    that built the table: the parquet in place is older than that row.
    """
    atp.import_atp()
    record_import(pipeline, "atp", LATER, "skipped", _version.app_version())
    built = oid(pipeline, "atp_places")

    atp.import_atp()

    assert oid(pipeline, "atp_places") == built


def test_a_failed_load_leaves_the_live_table_and_no_success(
    pipeline: Connection, parquet: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    atp.import_atp()
    before = atp_rows(pipeline)
    built = oid(pipeline, "atp_places")

    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    (parquet.parent / "spiders.json").unlink()  # the load fails after atp_places_new
    with pytest.raises(FileNotFoundError):
        atp.import_atp()

    assert oid(pipeline, "atp_places") == built
    assert atp_rows(pipeline) == before
    assert [r[1:] for r in imports(pipeline, "atp")] == [("success", _version.app_version())]


def test_no_parquet_is_an_error_not_a_skip(pipeline: Connection, atp_workdir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        atp.import_atp()


def test_a_leftover_new_table_is_started_over(pipeline: Connection, parquet: Path) -> None:
    """A crashed run left atp_places_new: the next one must not fail on it."""
    pipeline.execute("CREATE TABLE atp_places_new (x INT)")
    pipeline.commit()
    atp.import_atp()
    assert len(atp_rows(pipeline)) == 2


# =============================================================================
# nsi-download / nsi-import
# =============================================================================


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    """The npm registry's answer, and what download_nsi fetched."""
    state: dict[str, Any] = {"latest": "8.0.20260729", "fetched": []}

    def get(_url: str, timeout: float | None = None) -> _Json:
        if isinstance(state["latest"], Exception):
            raise state["latest"]
        return _Json({"dist-tags": {"latest": state["latest"]}})

    def download(url: str, path: Path) -> None:
        state["fetched"].append(url)
        # What the CDN serves under that URL: the release asked for, unless
        # a test says otherwise.
        served = state.get("served") or url.split("@")[1].split("/", maxsplit=1)[0]
        _nsi_file(Path(path), served, [])

    monkeypatch.setattr(nsi.requests, "get", get)
    monkeypatch.setattr(nsi, "download_large_file", download)
    monkeypatch.setattr(nsi, "NSI_DIR", tmp_path / "nsi")
    monkeypatch.setattr(nsi, "NSI_PATH", tmp_path / "nsi" / "nsi.json")
    return state


def test_the_same_release_by_the_same_revision_is_not_downloaded(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    record_import(pipeline, "nsi", TS, "success", nsi.stamp("8.0.20260729"))
    nsi.download_nsi()
    assert registry["fetched"] == []
    assert imports(pipeline, "nsi")[-1][1:] == ("skipped", nsi.stamp("8.0.20260729"))


def test_a_new_release_is_downloaded(pipeline: Connection, registry: dict[str, Any]) -> None:
    record_import(pipeline, "nsi", TS, "success", nsi.stamp("8.0.20260729"))
    registry["latest"] = "8.0.20260801"
    nsi.download_nsi()
    assert registry["fetched"] == [nsi.NSI_CDN_URL.format(version="8.0.20260801")]
    assert imports(pipeline, "nsi")[-1][1] == "pending"


def test_a_new_revision_downloads_the_same_release(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    record_import(pipeline, "nsi", TS, "success", "8.0.20260729+a-previous-revision")
    nsi.download_nsi()
    assert len(registry["fetched"]) == 1


def test_a_stale_file_from_the_cdn_is_an_outage_not_an_import(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    """JsDelivr once answered a moving tag from a years-old cache."""
    registry["latest"] = "8.0.20260801"
    registry["served"] = "6.0.20250817"
    with pytest.raises(SourceUnavailableError, match=r"served 6\.0\.20250817"):
        nsi.download_nsi()
    assert not nsi.NSI_PATH.exists()


def test_the_registry_unreachable_is_a_source_outage(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    registry["latest"] = requests.ConnectionError("dns")
    with pytest.raises(SourceUnavailableError):
        nsi.download_nsi()


def _nsi_file(path: Path, version: str, brands: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "_meta": {"version": version},
                "nsi": {
                    "brands/shop/clothes": {
                        "properties": {},
                        "templates": [],
                        "items": [
                            {
                                "displayName": brand,
                                "id": brand,
                                "locationSet": {"include": ["fr"]},
                                "tags": {"brand": brand, "brand:wikidata": qid, "shop": "clothes"},
                            }
                            for brand, qid in brands
                        ],
                    }
                },
            }
        )
    )


def nsi_brands(conn: Connection) -> list[tuple[Any, ...]]:
    conn.rollback()
    return conn.execute("SELECT brand FROM nsi_brands ORDER BY brand").fetchall()


def test_the_import_stamps_the_release_the_file_carries(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    """What is imported is what was downloaded: the registry is not asked
    again, a release in between would stamp the wrong one.
    """
    registry["latest"] = requests.ConnectionError("registry down since the download")
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])

    nsi.import_nsi()

    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi")[-1][1:] == ("success", nsi.stamp("8.0.20260729"))
    assert not nsi.NSI_PATH.exists()


def test_no_file_means_nothing_to_import(pipeline: Connection, registry: dict[str, Any]) -> None:
    pipeline.execute(
        "INSERT INTO nsi_brands (brand_wikidata, brand, name, primary_key, primary_value, tags)"
        " VALUES ('Q1', 'Babylone', 'Babylone', 'shop', 'clothes', '{}')"
    )
    pipeline.commit()
    nsi.import_nsi()
    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi") == []


def test_a_failed_import_leaves_the_previous_brands(
    pipeline: Connection, registry: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])
    nsi.import_nsi()

    _nsi_file(nsi.NSI_PATH, "8.0.20260801", [("Babylone", "Q1"), ("Nouvelle", "Q2")])

    def broken(_nsi_json: dict[str, Any]) -> Never:
        raise RuntimeError("unreadable release")

    monkeypatch.setattr(nsi, "select_items", broken)
    with pytest.raises(RuntimeError, match="unreadable"):
        nsi.import_nsi()

    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi")[-1][2] == nsi.stamp("8.0.20260729")
    # Kept for the retry.
    assert nsi.NSI_PATH.exists()


def test_a_release_replaces_the_previous_one_whole(
    pipeline: Connection, registry: dict[str, Any]
) -> None:
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])
    nsi.import_nsi()
    _nsi_file(nsi.NSI_PATH, "8.0.20260801", [("Nouvelle", "Q2")])
    nsi.import_nsi()
    assert nsi_brands(pipeline) == [("Nouvelle",)]


# =============================================================================
# mv-brand: the last step, and the disposal of the retired chain
# =============================================================================


@pytest.fixture
def refreshed(pipeline: Connection, parquet: Path, monkeypatch: pytest.MonkeyPatch) -> Connection:
    """A full first refresh: the OSM views and the ATP table are in, mv-brand
    has not run yet.
    """
    monkeypatch.setattr(atp2osm, "connect", osm.connect)
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    atp.import_atp()
    return pipeline


def relations(conn: Connection, pattern: str) -> set[str]:
    conn.rollback()
    return {
        r[0]
        for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
            " AND relkind IN ('r', 'm') AND relname ~ %s ORDER BY relname",
            (pattern,),
        ).fetchall()
    }


def test_the_brand_view_counts_the_matches_per_wave(refreshed: Connection) -> None:
    atp2osm.create_mv_places_brand()

    rows = refreshed.execute(
        "SELECT brand_wikidata, subdivision_code, wave, total FROM mv_places_brand ORDER BY wave"
    ).fetchall()
    # The Paris point carries Q1 and no phone; the ATP POI 500 m away brings
    # one: wave 1. Nothing to replace: no wave 2.
    assert rows == [("Q1", "75", 1, 1)]
    assert refreshed.execute("SELECT spider_id, matched FROM mv_places_spider").fetchall() == [
        ("babylone_fr", 1)
    ]


def test_unchanged_inputs_skip_the_brand_view(refreshed: Connection) -> None:
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")
    atp2osm.create_mv_places_brand()
    assert oid(refreshed, "mv_places_brand") == built


def _osm_data_moved(conn: Connection, _mp: pytest.MonkeyPatch) -> None:
    record_import(conn, "osm", LATER, "success")


def _atp_data_moved(conn: Connection, _mp: pytest.MonkeyPatch) -> None:
    # The ATP date is the parquet's mtime, today's: only a later one moves it.
    record_import(
        conn, "atp", datetime.now(UTC) + timedelta(days=1), "success", _version.app_version()
    )


def _nsi_release_moved(conn: Connection, _mp: pytest.MonkeyPatch) -> None:
    nsi_imported(conn, "8.0.20260801")


def _revision_moved(_conn: Connection, mp: pytest.MonkeyPatch) -> None:
    mp.setattr(atp2osm, "app_version", lambda: "next-deploy")


@pytest.mark.parametrize(
    "move",
    [
        _osm_data_moved,
        _atp_data_moved,
        _nsi_release_moved,
        _revision_moved,
    ],
    ids=["osm-data", "atp-data", "nsi-release", "revision"],
)
def test_any_input_moving_rebuilds_the_brand_view(
    refreshed: Connection, monkeypatch: pytest.MonkeyPatch, move: Move
) -> None:
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")

    move(refreshed, monkeypatch)
    atp2osm.create_mv_places_brand()

    assert oid(refreshed, "mv_places_brand") != built


def test_the_whole_retired_chain_goes_once_the_brand_view_is_swapped(
    refreshed: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full second refresh retires points, polygons, subdivisions, mv_places,
    atp_places and atp_spiders; nothing reads them once the brand view is
    rebuilt, and mv-brand disposes of them all.
    """
    atp2osm.create_mv_places_brand()
    osm.run_osm2pgsql()
    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()
    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    atp.import_atp()
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")
    assert relations(refreshed, "_old") == {
        "points_old",
        "polygons_old",
        "subdivisions_old",
        "mv_places_old",
        "atp_places_old",
        "atp_spiders_old",
    }

    atp2osm.create_mv_places_brand()

    assert relations(refreshed, "_old") == set()
    # The live chain is whole.
    assert count(refreshed, "SELECT count(*) FROM mv_places_brand") == 1


def test_a_retired_table_still_read_is_kept(
    refreshed: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Points reimported, mv_places not rebuilt yet: the view still reads
    points_old and polygons_old, which must survive the disposal. The
    boundaries, which nothing reads, go.
    """
    atp2osm.create_mv_places_brand()
    osm.run_osm2pgsql()
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")

    atp2osm.create_mv_places_brand()

    assert relations(refreshed, "_old") == {"points_old", "polygons_old"}
    assert count(refreshed, "SELECT count(*) FROM mv_places") == 2


def test_a_failed_brand_view_leaves_the_live_one_and_the_retired_chain(
    refreshed: Connection, osm2pgsql: list[Osm2pgsqlCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")
    osm.run_osm2pgsql()
    retired = relations(refreshed, "_old")
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")

    monkeypatch.setattr(atp2osm, "mv_places_spider_sql", _broken_view)

    with pytest.raises(psycopg.Error):
        atp2osm.create_mv_places_brand()

    assert oid(refreshed, "mv_places_brand") == built
    assert relations(refreshed, "_old") == retired


# =============================================================================
# The site reads what the pipeline built
# =============================================================================


def test_the_review_reads_its_proposals_off_the_views(refreshed: Connection) -> None:
    """brand_matches, unstaged: the matching SQL on the tables and views the
    refresh just built, through to the proposals the review page shows.
    """
    from psycopg.rows import dict_row

    from src.matching import get_changes, get_filtered

    atp2osm.create_mv_places_brand()
    with refreshed.cursor(row_factory=dict_row) as cur:
        get_filtered(cur, brand="Q1")
        changes = get_changes(cur, wave=1)

    (change,) = changes
    assert (change["node_type"], change["id"]) == ("node", 101)
    assert change["tag"]["phone"] == "+33 1 00 00 00 00"
    assert change["old_tag"] == {"name": "Babylone", "shop": "clothes", "brand:wikidata": "Q1"}
    assert (change["subdivision_code"], change["subdivision_name"]) == ("75", "Paris")
    assert change["osm_timestamp"] is not None
