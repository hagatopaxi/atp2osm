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
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import requests

from src.config import Database
from src.phone import ensure_normalize_phone
from src.pipeline import _matview, _version, atp, atp2osm, nsi, osm
from src.pipeline._db import last_import_comment, last_import_date, record_import, start_import
from src.pipeline.errors import SourceUnavailable
from src.pipeline.ndgeojson_to_parquet import convert_to_parquet

TS = datetime(2026, 8, 27, tzinfo=timezone.utc)
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
def pipeline(migrated_conn, db_kwargs, monkeypatch, tmp_path):
    """The pipeline on the throwaway database, with the OSM tables of a
    previous import in place, and every file it writes under tmp_path."""
    # Two ways in: the psycopg connections, and the settings DuckDB and
    # osm2pgsql are handed to reach the same database.
    test_db = Database(name=db_kwargs["dbname"], user=db_kwargs["user"],
                       password=db_kwargs["password"], host=db_kwargs["host"],
                       port=db_kwargs["port"])
    for module in (osm, atp, nsi):
        monkeypatch.setattr(module, "connect", lambda: psycopg.connect(**db_kwargs))
    for module in (osm, atp):
        monkeypatch.setattr(module, "get_database", lambda: test_db)
    monkeypatch.setattr(osm, "GEOFABRIK_TS_PATH", tmp_path / "osm" / "geofabrik-timestamp.txt")
    monkeypatch.setattr(osm, "GEOFABRIK_REGIONS", {
        "france": {
            "url": "https://geofabrik.example/france-latest.osm.pbf",
            "state_url": "https://geofabrik.example/france-updates/state.txt",
            "pbf_path": tmp_path / "osm" / "france-latest.osm.pbf",
        },
    })

    conn = migrated_conn
    ensure_normalize_phone(conn)
    conn.execute("DROP TABLE IF EXISTS atp_places, atp_spiders")
    conn.execute(OSM_TABLES_SQL.format(schema="public"))
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
            cur.execute(f"DROP {'MATERIALIZED VIEW' if kind == 'm' else 'TABLE'} IF EXISTS {name} CASCADE")
        cur.execute(f"DROP SCHEMA IF EXISTS {osm.IMPORT_SCHEMA} CASCADE")
        cur.execute("""
            CREATE TABLE atp_places (id TEXT, spider_id TEXT, brand_wikidata TEXT, brand TEXT);
            CREATE TABLE atp_spiders (spider TEXT, filename TEXT, errors INT8, features INT8,
                                      elapsed_time FLOAT8, updated_at TIMESTAMPTZ);
        """)
    conn.commit()


def imports(conn, kind):
    """(date, status, comment) of every row of that datasource, oldest first."""
    return conn.execute(
        "SELECT date, status, comment FROM data_imports WHERE type = %s ORDER BY id",
        (kind,),
    ).fetchall()


def oid(conn, name):
    # A name resolved inside an open transaction is the one that transaction
    # first saw: the step swapped on a connection of its own, so look afresh.
    conn.rollback()
    return conn.execute("SELECT to_regclass(%s)::oid", (name,)).fetchone()[0]


def stamp(conn, name, sig, kind="TABLE"):
    with conn.cursor() as cur:
        _matview.stamp(cur, name, sig, kind)
    conn.commit()


def geofabrik(monkeypatch, newest):
    monkeypatch.setattr(osm, "_newest_geofabrik_timestamp", lambda: newest)


# =============================================================================
# osm-download
# =============================================================================


@pytest.fixture
def downloads(monkeypatch):
    """What download_pbf fetched: (url, path) per call. Writes a file."""
    calls = []

    def download(url, path, session=None):
        calls.append((url, Path(path)))
        Path(path).write_bytes(b"pbf")

    monkeypatch.setattr(osm, "download_large_file", download)
    return calls


def test_nothing_new_and_the_same_revision_skips_the_download(pipeline, monkeypatch, downloads):
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", _version.app_version())
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert downloads == []
    assert imports(pipeline, "osm")[-1] == (TS, "skipped", None)


def test_newer_data_is_downloaded_and_the_row_stays_open(pipeline, monkeypatch, downloads):
    """The row opened here is resolved by osm-views, once the tables are in."""
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", _version.app_version())
    geofabrik(monkeypatch, LATER)

    osm.download_pbf()

    assert [url for url, _ in downloads] == ["https://geofabrik.example/france-latest.osm.pbf"]
    assert osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()
    assert imports(pipeline, "osm")[-1][1] == "pending"


def test_a_new_revision_downloads_without_new_data(pipeline, monkeypatch, downloads):
    record_import(pipeline, "osm", TS, "success")
    stamp(pipeline, "points", "a-previous-revision")
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert len(downloads) == 1


def test_tables_that_were_never_stamped_are_reimported(pipeline, monkeypatch, downloads):
    """A database from before the guards: no stamp is not the current stamp."""
    record_import(pipeline, "osm", TS, "success")
    geofabrik(monkeypatch, TS)
    osm.download_pbf()
    assert len(downloads) == 1


def test_a_first_run_downloads(pipeline, monkeypatch, downloads):
    geofabrik(monkeypatch, TS)
    osm.download_pbf()
    assert len(downloads) == 1


def test_a_pbf_already_on_disk_is_not_fetched_again(pipeline, monkeypatch, downloads):
    """A run that crashed after the download resumes from the file."""
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]
    pbf.parent.mkdir(parents=True)
    pbf.write_bytes(b"pbf")
    geofabrik(monkeypatch, TS)

    osm.download_pbf()

    assert downloads == []


def test_a_failed_download_leaves_no_partial_file(pipeline, monkeypatch):
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]

    def download(url, path, session=None):
        Path(path).write_bytes(b"half a pl")
        raise requests.ConnectionError("reset by peer")

    monkeypatch.setattr(osm, "download_large_file", download)
    geofabrik(monkeypatch, TS)

    with pytest.raises(requests.ConnectionError):
        osm.download_pbf()

    # Otherwise the next run would take the stump for the whole extract.
    assert not pbf.exists()


def test_geofabrik_down_opens_no_row(pipeline, monkeypatch, downloads):
    geofabrik(monkeypatch, None)
    with pytest.raises(SourceUnavailable):
        osm.download_pbf()
    assert imports(pipeline, "osm") == []
    assert downloads == []


# =============================================================================
# osm-import
# =============================================================================


@pytest.fixture
def osm2pgsql(pipeline, db_kwargs, monkeypatch):
    """osm2pgsql as a function: writes the tables into the import schema, the
    way the real one does with --create, and records how it was called."""
    calls = []

    def run(args, check, env):
        calls.append({"args": args, "env": env})
        with psycopg.connect(**db_kwargs) as c:
            c.execute(OSM_TABLES_SQL.format(schema=env["ATP2OSM_IMPORT_SCHEMA"]))
            # A marker telling the new table from the old one.
            c.execute(f"INSERT INTO {env['ATP2OSM_IMPORT_SCHEMA']}.points VALUES"
                      " (999, '{\"name\": \"fresh\"}', ST_SetSRID(ST_Point(0, 0), 4326), 1, 1)")
            c.commit()

    monkeypatch.setattr(osm.subprocess, "run", run)
    monkeypatch.setattr(osm.shutil, "disk_usage", lambda p: type("u", (), {"free": 10**12})())
    pbf = osm.GEOFABRIK_REGIONS["france"]["pbf_path"]
    pbf.parent.mkdir(parents=True, exist_ok=True)
    pbf.write_bytes(b"pbf")
    return calls


def test_the_import_swaps_the_new_tables_in_and_retires_the_old(pipeline, osm2pgsql):
    before = {t: oid(pipeline, t) for t in osm.OSM_TABLES}

    osm.run_osm2pgsql()

    (call,) = osm2pgsql
    assert call["env"]["ATP2OSM_IMPORT_SCHEMA"] == osm.IMPORT_SCHEMA
    assert call["env"]["ATP2OSM_ADMIN_LEVEL_MAX"] == str(osm.ADMIN_LEVEL_MAX)
    assert "PGPASSWORD" in call["env"]
    assert "-x" in call["args"] and "flex" in call["args"]
    for table in osm.OSM_TABLES:
        assert oid(pipeline, table) != before[table], f"{table} was not swapped"
        assert oid(pipeline, f"{table}_old") == before[table], f"{table} was dropped, not retired"
    assert pipeline.execute("SELECT count(*) FROM points WHERE node_id = 999").fetchone()[0] == 1
    assert _matview.is_current(pipeline, "points", _version.app_version())
    assert oid(pipeline, f"{osm.IMPORT_SCHEMA}.points") is None
    assert not osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()
    # The pieces are cut from the new boundaries.
    assert _matview.is_current(
        pipeline, "subdivision_parts",
        _matview.signature(_version.app_version(), oid(pipeline, "subdivisions")),
    )


def test_a_view_on_the_old_tables_keeps_serving_through_the_swap(pipeline, osm2pgsql):
    pipeline.execute("CREATE MATERIALIZED VIEW mv_places AS SELECT node_id FROM points")
    pipeline.commit()

    osm.run_osm2pgsql()

    assert pipeline.execute("SELECT count(*) FROM mv_places").fetchone()[0] == 2


def test_a_failed_osm2pgsql_leaves_the_live_tables_and_the_pbf(pipeline, osm2pgsql, monkeypatch):
    before = {t: oid(pipeline, t) for t in osm.OSM_TABLES}

    def fail(args, check, env):
        raise subprocess.CalledProcessError(1, "osm2pgsql")

    monkeypatch.setattr(osm.subprocess, "run", fail)

    with pytest.raises(subprocess.CalledProcessError):
        osm.run_osm2pgsql()

    for table in osm.OSM_TABLES:
        assert oid(pipeline, table) == before[table]
    # Retried from here, without downloading again.
    assert osm.GEOFABRIK_REGIONS["france"]["pbf_path"].exists()


def test_a_full_disk_fails_before_osm2pgsql_starts(pipeline, osm2pgsql, monkeypatch):
    monkeypatch.setattr(osm.shutil, "disk_usage", lambda p: type("u", (), {"free": 10**9})())
    with pytest.raises(RuntimeError, match="free disk"):
        osm.run_osm2pgsql()
    assert osm2pgsql == []


def test_the_pieces_are_recut_only_when_the_boundaries_moved(pipeline, osm2pgsql):
    osm.run_osm2pgsql()
    pieces = oid(pipeline, "subdivision_parts")

    osm._build_subdivision_parts()
    assert oid(pipeline, "subdivision_parts") == pieces

    osm.GEOFABRIK_REGIONS["france"]["pbf_path"].write_bytes(b"pbf")
    osm.run_osm2pgsql()
    assert oid(pipeline, "subdivision_parts") != pieces


def test_a_leftover_import_schema_is_started_over(pipeline, osm2pgsql):
    """A crashed run leaves its schema: --create would fail on the tables in it."""
    pipeline.execute(f"CREATE SCHEMA {osm.IMPORT_SCHEMA}; CREATE TABLE {osm.IMPORT_SCHEMA}.points (x INT)")
    pipeline.commit()
    osm.run_osm2pgsql()
    assert pipeline.execute("SELECT count(*) FROM points WHERE node_id = 999").fetchone()[0] == 1


# =============================================================================
# osm-views
# =============================================================================


def nsi_imported(conn, version="8.0.20260729"):
    record_import(conn, "nsi", TS, "success", nsi._stamp(version))


def test_the_first_build_creates_the_view_and_resolves_the_row(pipeline, monkeypatch):
    nsi_imported(pipeline)
    start_import(pipeline, "osm")
    geofabrik(monkeypatch, TS)

    osm.setup_mv_places()

    assert imports(pipeline, "osm") == [(TS, "success", None)]
    rows = pipeline.execute("SELECT osm_id, node_type FROM mv_places ORDER BY osm_id").fetchall()
    # The crossing carries nothing a match can key on.
    assert rows == [(101, "node"), (201, "way")]
    indexes = {
        r[0] for r in pipeline.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'mv_places'"
        ).fetchall()
    }
    assert indexes == set(osm.MV_PLACES_INDEXES)


def test_unchanged_inputs_skip_the_rebuild(pipeline, monkeypatch):
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") == built
    assert imports(pipeline, "osm")[-1][1] == "skipped"


def test_newer_osm_data_rebuilds(pipeline, monkeypatch):
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built
    assert oid(pipeline, "mv_places_old") == built
    assert imports(pipeline, "osm")[-1] == (LATER, "success", None)


def test_a_new_nsi_release_rebuilds_without_new_osm_data(pipeline, monkeypatch):
    """The view completes brand:wikidata from nsi_brands: it is an input."""
    nsi_imported(pipeline, "8.0.20260729")
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    nsi_imported(pipeline, "8.0.20260801")
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built


def test_a_new_revision_rebuilds_without_new_data(pipeline, monkeypatch):
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    monkeypatch.setattr(osm, "app_version", lambda: "next-deploy")
    osm.setup_mv_places()

    assert oid(pipeline, "mv_places") != built


def test_geofabrik_down_still_rebuilds_on_the_other_inputs(pipeline, monkeypatch):
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


def test_geofabrik_down_on_a_first_run_builds_nothing(pipeline, monkeypatch):
    geofabrik(monkeypatch, None)
    osm.setup_mv_places()
    assert oid(pipeline, "mv_places") is None
    assert imports(pipeline, "osm") == []


def test_a_failed_build_leaves_the_live_view_and_records_no_success(pipeline, monkeypatch):
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    built = oid(pipeline, "mv_places")

    geofabrik(monkeypatch, LATER)
    monkeypatch.setattr(osm, "_mv_places_sql", lambda name: f"CREATE MATERIALIZED VIEW {name} AS SELECT 1/0")
    with pytest.raises(psycopg.Error):
        osm.setup_mv_places()

    assert oid(pipeline, "mv_places") == built
    assert oid(pipeline, "mv_places_new") is None
    assert imports(pipeline, "osm")[-1] == (TS, "success", None)


def test_a_new_view_is_built_from_the_new_tables(pipeline, osm2pgsql, monkeypatch):
    """After osm-import, osm-views reads points, not points_old."""
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    osm.run_osm2pgsql()

    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()

    assert pipeline.execute("SELECT count(*) FROM mv_places WHERE osm_id = 999").fetchone()[0] == 1


# =============================================================================
# atp-download
# =============================================================================


class _Json:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def run(run_id, end_time):
    return {
        "run_id": run_id,
        "end_time": end_time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parquet_url": f"https://atp.example/{run_id}/output.parquet",
        "output_url": f"https://atp.example/{run_id}/output.zip",
        "stats_url": f"https://atp.example/{run_id}/stats.json",
    }


@pytest.fixture
def atp_workdir(pipeline, monkeypatch, tmp_path):
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
def atp_history(monkeypatch):
    """ATP's run history, oldest first as it is published, and the files
    download_atp fetched."""
    state = {"runs": [], "fetched": []}

    def get(url, timeout=None):
        if isinstance(state["runs"], Exception):
            raise state["runs"]
        return _Json(state["runs"])

    def download(url, path):
        state["fetched"].append(url)
        if url.endswith("stats.json"):
            Path(path).write_text(json.dumps({"results": [
                {"spider": "babylone_fr", "filename": "locations/spiders/babylone_fr.py",
                 "errors": 0, "features": 2, "elapsed_time": 1.5},
            ]}))
        else:
            Path(path).write_bytes(b"zip")

    monkeypatch.setattr(atp.requests, "get", get)
    monkeypatch.setattr(atp, "download_large_file", download)
    monkeypatch.setattr(atp, "spider_dates", lambda: {
        "locations/spiders/babylone_fr.py": "2026-08-01T00:00:00+00:00",
    })
    return state


def test_nothing_new_from_atp_skips_and_keeps_the_stamp(pipeline, atp_workdir, atp_history):
    """The row says 'skipped' with the comment already there: what describes
    the table in place is the revision that built it, not the one running."""
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


def test_a_new_run_is_downloaded_with_its_dated_spiders(pipeline, atp_workdir, atp_history):
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


def test_a_first_run_takes_the_newest(pipeline, atp_workdir, atp_history):
    atp_history["runs"] = [run("r1", EARLIER), run("r2", TS)]
    atp.download_atp()
    assert atp_history["fetched"][0] == "https://atp.example/r2/output.zip"


def test_atp_unreachable_is_a_source_outage(pipeline, atp_workdir, atp_history):
    atp_history["runs"] = requests.ConnectionError("dns")
    with pytest.raises(SourceUnavailable):
        atp.download_atp()
    # The row is left open for the runner to resolve as skipped.
    assert imports(pipeline, "atp")[-1][1] == "pending"


def test_github_down_costs_the_dates_not_the_run(pipeline, atp_workdir, atp_history, monkeypatch):
    atp_history["runs"] = [run("r3", LATER)]

    def no_git():
        raise subprocess.CalledProcessError(128, "git")

    monkeypatch.setattr(atp, "spider_dates", no_git)

    atp.download_atp()

    (spider,) = json.loads((atp_workdir / "spiders.json").read_text())
    assert spider["updated_at"] is None


# =============================================================================
# atp-import
# =============================================================================


def feature(id, spider, country, lon, lat, **props):
    return {
        "type": "Feature",
        "id": id,
        "properties": {
            "@spider": spider, "addr:country": country, "brand": "Babylone",
            "brand:wikidata": "Q1", "name": f"Babylone {id}",
            "email": "SHOP@Babylone.example", **props,
        },
        "geometry": None if lon is None else {"type": "Point", "coordinates": [lon, lat]},
    }


@pytest.fixture
def parquet(atp_workdir):
    """A parquet built by the pipeline's own converter, from features of
    three countries, one of them at sea, one of them without a location."""
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
    (atp_workdir / "spiders.json").write_text(json.dumps([
        {"spider": "babylone_fr", "filename": "locations/spiders/babylone_fr.py",
         "errors": 0, "features": 3, "elapsed_time": 1.5, "updated_at": "2026-08-01T00:00:00+00:00"},
        {"spider": "babylone_mq", "filename": "locations/spiders/babylone_mq.py",
         "errors": 0, "features": 1, "elapsed_time": 0.5, "updated_at": None},
        {"spider": "babylone_de", "filename": "locations/spiders/babylone_de.py",
         "errors": 0, "features": 1, "elapsed_time": 0.5, "updated_at": None},
    ]))
    return atp_workdir / "latest.parquet"


def atp_rows(conn):
    conn.rollback()
    return conn.execute(
        "SELECT id, subdivision_code, subdivision_name, email FROM atp_places ORDER BY id"
    ).fetchall()


def test_the_import_keeps_the_country_and_attaches_every_poi(pipeline, parquet):
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
        r[0] for r in pipeline.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'atp_places'"
        ).fetchall()
    }
    assert indexes == set(atp.ATP_PLACES_INDEXES)
    spiders = pipeline.execute("SELECT spider, updated_at FROM atp_spiders ORDER BY spider").fetchall()
    assert [s[0] for s in spiders] == ["babylone_fr", "babylone_mq"]
    assert spiders[0][1] is not None
    (recorded,) = imports(pipeline, "atp")
    assert recorded[1:] == ("success", _version.app_version())
    assert recorded[0] == datetime.fromtimestamp(parquet.stat().st_mtime, tz=timezone.utc)


def test_the_same_parquet_by_the_same_revision_is_not_imported_twice(pipeline, parquet):
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    atp.import_atp()

    assert oid(pipeline, "atp_places") == built
    assert len(imports(pipeline, "atp")) == 1


def test_a_new_revision_reimports_the_same_parquet(pipeline, parquet, monkeypatch):
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    atp.import_atp()

    assert oid(pipeline, "atp_places") != built
    assert imports(pipeline, "atp")[-1][2] == "next-deploy"


def test_a_newer_parquet_is_imported(pipeline, parquet):
    atp.import_atp()
    built = oid(pipeline, "atp_places")

    import os
    later = parquet.stat().st_mtime + 3600
    os.utime(parquet, (later, later))
    atp.import_atp()

    assert oid(pipeline, "atp_places") != built


def test_a_skipped_download_is_not_re_imported(pipeline, parquet):
    """download_atp found nothing new and stamped the row with the revision
    that built the table: the parquet in place is older than that row."""
    atp.import_atp()
    record_import(pipeline, "atp", LATER, "skipped", _version.app_version())
    built = oid(pipeline, "atp_places")

    atp.import_atp()

    assert oid(pipeline, "atp_places") == built


def test_a_failed_load_leaves_the_live_table_and_no_success(pipeline, parquet, monkeypatch):
    atp.import_atp()
    before = atp_rows(pipeline)
    built = oid(pipeline, "atp_places")

    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    (parquet.parent / "spiders.json").unlink()  # the load fails after atp_places_new
    with pytest.raises(Exception):
        atp.import_atp()

    assert oid(pipeline, "atp_places") == built
    assert atp_rows(pipeline) == before
    assert [r[1:] for r in imports(pipeline, "atp")] == [("success", _version.app_version())]


def test_no_parquet_is_an_error_not_a_skip(pipeline, atp_workdir):
    with pytest.raises(FileNotFoundError):
        atp.import_atp()


def test_a_leftover_new_table_is_started_over(pipeline, parquet):
    """A crashed run left atp_places_new: the next one must not fail on it."""
    pipeline.execute("CREATE TABLE atp_places_new (x INT)")
    pipeline.commit()
    atp.import_atp()
    assert len(atp_rows(pipeline)) == 2


# =============================================================================
# nsi-download / nsi-import
# =============================================================================


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """The npm registry's answer, and what download_nsi fetched."""
    state = {"latest": "8.0.20260729", "fetched": []}

    def get(url, timeout=None):
        if isinstance(state["latest"], Exception):
            raise state["latest"]
        return _Json({"dist-tags": {"latest": state["latest"]}})

    def download(url, path):
        state["fetched"].append(url)
        # What the CDN serves under that URL: the release asked for, unless
        # a test says otherwise.
        served = state.get("served") or url.split("@")[1].split("/")[0]
        _nsi_file(Path(path), served, [])

    monkeypatch.setattr(nsi.requests, "get", get)
    monkeypatch.setattr(nsi, "download_large_file", download)
    monkeypatch.setattr(nsi, "NSI_DIR", tmp_path / "nsi")
    monkeypatch.setattr(nsi, "NSI_PATH", tmp_path / "nsi" / "nsi.json")
    return state


def test_the_same_release_by_the_same_revision_is_not_downloaded(pipeline, registry):
    record_import(pipeline, "nsi", TS, "success", nsi._stamp("8.0.20260729"))
    nsi.download_nsi()
    assert registry["fetched"] == []
    assert imports(pipeline, "nsi")[-1][1:] == ("skipped", nsi._stamp("8.0.20260729"))


def test_a_new_release_is_downloaded(pipeline, registry):
    record_import(pipeline, "nsi", TS, "success", nsi._stamp("8.0.20260729"))
    registry["latest"] = "8.0.20260801"
    nsi.download_nsi()
    assert registry["fetched"] == [nsi.NSI_CDN_URL.format(version="8.0.20260801")]
    assert imports(pipeline, "nsi")[-1][1] == "pending"


def test_a_new_revision_downloads_the_same_release(pipeline, registry):
    record_import(pipeline, "nsi", TS, "success", "8.0.20260729+a-previous-revision")
    nsi.download_nsi()
    assert len(registry["fetched"]) == 1


def test_a_stale_file_from_the_cdn_is_an_outage_not_an_import(pipeline, registry):
    """jsDelivr once answered a moving tag from a years-old cache."""
    registry["latest"] = "8.0.20260801"
    registry["served"] = "6.0.20250817"
    with pytest.raises(SourceUnavailable, match="served 6.0.20250817"):
        nsi.download_nsi()
    assert not nsi.NSI_PATH.exists()


def test_the_registry_unreachable_is_a_source_outage(pipeline, registry):
    registry["latest"] = requests.ConnectionError("dns")
    with pytest.raises(SourceUnavailable):
        nsi.download_nsi()


def _nsi_file(path, version, brands):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_meta": {"version": version},
        "nsi": {"brands/shop/clothes": {"properties": {}, "templates": [], "items": [
            {"displayName": brand, "id": brand, "locationSet": {"include": ["fr"]},
             "tags": {"brand": brand, "brand:wikidata": qid, "shop": "clothes"}}
            for brand, qid in brands
        ]}},
    }))


def nsi_brands(conn):
    conn.rollback()
    return conn.execute("SELECT brand FROM nsi_brands ORDER BY brand").fetchall()


def test_the_import_stamps_the_release_the_file_carries(pipeline, registry):
    """What is imported is what was downloaded: the registry is not asked
    again, a release in between would stamp the wrong one."""
    registry["latest"] = requests.ConnectionError("registry down since the download")
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])

    nsi.import_nsi()

    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi")[-1][1:] == ("success", nsi._stamp("8.0.20260729"))
    assert not nsi.NSI_PATH.exists()


def test_no_file_means_nothing_to_import(pipeline, registry):
    pipeline.execute("INSERT INTO nsi_brands (brand_wikidata, brand, name, primary_key, primary_value, tags)"
                     " VALUES ('Q1', 'Babylone', 'Babylone', 'shop', 'clothes', '{}')")
    pipeline.commit()
    nsi.import_nsi()
    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi") == []


def test_a_failed_import_leaves_the_previous_brands(pipeline, registry, monkeypatch):
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])
    nsi.import_nsi()

    _nsi_file(nsi.NSI_PATH, "8.0.20260801", [("Babylone", "Q1"), ("Nouvelle", "Q2")])

    def broken(nsi_json):
        raise RuntimeError("unreadable release")

    monkeypatch.setattr(nsi, "select_items", broken)
    with pytest.raises(RuntimeError):
        nsi.import_nsi()

    assert nsi_brands(pipeline) == [("Babylone",)]
    assert imports(pipeline, "nsi")[-1][2] == nsi._stamp("8.0.20260729")
    # Kept for the retry.
    assert nsi.NSI_PATH.exists()


def test_a_release_replaces_the_previous_one_whole(pipeline, registry):
    _nsi_file(nsi.NSI_PATH, "8.0.20260729", [("Babylone", "Q1")])
    nsi.import_nsi()
    _nsi_file(nsi.NSI_PATH, "8.0.20260801", [("Nouvelle", "Q2")])
    nsi.import_nsi()
    assert nsi_brands(pipeline) == [("Nouvelle",)]


# =============================================================================
# mv-brand: the last step, and the disposal of the retired chain
# =============================================================================


@pytest.fixture
def refreshed(pipeline, parquet, monkeypatch):
    """A full first refresh: the OSM views and the ATP table are in, mv-brand
    has not run yet."""
    monkeypatch.setattr(atp2osm, "connect", osm.connect)
    nsi_imported(pipeline)
    geofabrik(monkeypatch, TS)
    osm.setup_mv_places()
    atp.import_atp()
    return pipeline


def relations(conn, pattern):
    conn.rollback()
    return {
        r[0] for r in conn.execute(
            "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace"
            " AND relkind IN ('r', 'm') AND relname ~ %s ORDER BY relname",
            (pattern,),
        ).fetchall()
    }


def test_the_brand_view_counts_the_matches_per_wave(refreshed):
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


def test_unchanged_inputs_skip_the_brand_view(refreshed):
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")
    atp2osm.create_mv_places_brand()
    assert oid(refreshed, "mv_places_brand") == built


@pytest.mark.parametrize(
    "move",
    [
        lambda conn, mp: record_import(conn, "osm", LATER, "success"),
        # The ATP date is the parquet's mtime, today's: only a later one moves it.
        lambda conn, mp: record_import(
            conn, "atp", datetime.now(timezone.utc) + timedelta(days=1), "success", _version.app_version()
        ),
        lambda conn, mp: nsi_imported(conn, "8.0.20260801"),
        lambda conn, mp: mp.setattr(atp2osm, "app_version", lambda: "next-deploy"),
    ],
    ids=["osm-data", "atp-data", "nsi-release", "revision"],
)
def test_any_input_moving_rebuilds_the_brand_view(refreshed, monkeypatch, move):
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")

    move(refreshed, monkeypatch)
    atp2osm.create_mv_places_brand()

    assert oid(refreshed, "mv_places_brand") != built


def test_the_whole_retired_chain_goes_once_the_brand_view_is_swapped(refreshed, osm2pgsql, monkeypatch):
    """A full second refresh retires points, polygons, subdivisions, mv_places,
    atp_places and atp_spiders; nothing reads them once the brand view is
    rebuilt, and mv-brand disposes of them all."""
    atp2osm.create_mv_places_brand()
    osm.run_osm2pgsql()
    geofabrik(monkeypatch, LATER)
    osm.setup_mv_places()
    monkeypatch.setattr(atp, "app_version", lambda: "next-deploy")
    atp.import_atp()
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")
    assert relations(refreshed, "_old") == {
        "points_old", "polygons_old", "subdivisions_old",
        "mv_places_old", "atp_places_old", "atp_spiders_old",
    }

    atp2osm.create_mv_places_brand()

    assert relations(refreshed, "_old") == set()
    # The live chain is whole.
    assert refreshed.execute("SELECT count(*) FROM mv_places_brand").fetchone()[0] == 1


def test_a_retired_table_still_read_is_kept(refreshed, osm2pgsql, monkeypatch):
    """points reimported, mv_places not rebuilt yet: the view still reads
    points_old and polygons_old, which must survive the disposal. The
    boundaries, which nothing reads, go."""
    atp2osm.create_mv_places_brand()
    osm.run_osm2pgsql()
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")

    atp2osm.create_mv_places_brand()

    assert relations(refreshed, "_old") == {"points_old", "polygons_old"}
    assert refreshed.execute("SELECT count(*) FROM mv_places").fetchone()[0] == 2


def test_a_failed_brand_view_leaves_the_live_one_and_the_retired_chain(refreshed, osm2pgsql, monkeypatch):
    atp2osm.create_mv_places_brand()
    built = oid(refreshed, "mv_places_brand")
    osm.run_osm2pgsql()
    retired = relations(refreshed, "_old")
    monkeypatch.setattr(atp2osm, "app_version", lambda: "next-deploy")
    monkeypatch.setattr(atp2osm, "_mv_places_spider_sql",
                        lambda name: f"CREATE MATERIALIZED VIEW {name} AS SELECT 1/0")

    with pytest.raises(psycopg.Error):
        atp2osm.create_mv_places_brand()

    assert oid(refreshed, "mv_places_brand") == built
    assert relations(refreshed, "_old") == retired
