import logging
import os
import shutil
import subprocess
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import LiteralString

import requests
from psycopg import sql
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import get_database, get_pipeline
from src.db import code_sql
from src.pipeline import _matview
from src.pipeline._db import (
    connect,
    last_import_comment,
    last_import_date,
    record_import,
    start_import,
)
from src.pipeline._version import app_version
from src.pipeline.constants import (
    ADMIN_LEVEL_MAX,
    GEOFABRIK_REGIONS,
    GEOFABRIK_TS_PATH,
    PROJECT_ROOT,
    Region,
)
from src.pipeline.errors import SourceUnavailableError
from src.utils import delete_file_if_exists, download_large_file

logger = logging.getLogger(__name__)

# Geofabrik hands out 502/503 for a few minutes at a time, and sometimes lets
# a TLS handshake hang. Retry with backoff instead of failing the whole
# nightly run on a transient blip. Connection errors are retried too; a read
# timeout mid-stream is not, it would restart a multi-GB download from scratch.
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=5,
            connect=5,
            read=0,
            backoff_factor=5,  # waits 0, 5, 10, 20, 40s
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
        )
    ),
)


def geofabrik_timestamp(region: Region) -> datetime:
    """Fetch the data timestamp for a region.

    Tries the Geofabrik state.txt first; falls back to the HTTP Last-Modified
    header of the PBF file for regions that don't publish a state file.
    """
    try:
        resp = _session.get(region["state_url"], timeout=30)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if line.startswith("timestamp="):
                ts = line[len("timestamp=") :].replace("\\:", ":")
                return datetime.fromisoformat(ts)
    except (requests.RequestException, ValueError):
        logger.debug("No state.txt for %s, reading Last-Modified", region["url"], exc_info=True)

    # Fallback: Last-Modified header on the PBF file
    resp = _session.head(region["url"], timeout=30, allow_redirects=True)
    resp.raise_for_status()
    last_modified = resp.headers.get("Last-Modified")
    if last_modified:
        return parsedate_to_datetime(last_modified)

    raise ValueError(f"Cannot determine data timestamp for {region['url']}")


def forget_geofabrik_timestamp() -> None:
    """Drop what a previous run left behind. Called at the start of every run."""
    delete_file_if_exists(GEOFABRIK_TS_PATH)


def newest_geofabrik_timestamp() -> datetime | None:
    """Return the most recent timestamp across all configured regions.

    Answered once per run and parked in GEOFABRIK_TS_PATH: osm-probe,
    osm-download and osm-views all need it, minutes apart, and the retries make
    each round trip cost minutes when Geofabrik is slow. Sharing one answer
    also keeps the run coherent — osm-views cannot decide on a date
    osm-download never saw. An empty file means "asked, and it was down".

    We refresh when any region has data newer than our last import,
    so we compare last_import_date against the maximum (newest) timestamp.
    """
    if GEOFABRIK_TS_PATH.exists():
        parked = GEOFABRIK_TS_PATH.read_text().strip()
        return datetime.fromisoformat(parked) if parked else None

    timestamps: list[datetime] = []
    for name, region in GEOFABRIK_REGIONS.items():
        try:
            timestamps.append(geofabrik_timestamp(region))
        except (requests.RequestException, ValueError):
            logger.exception("Could not fetch timestamp for %s", name)
    newest = max(timestamps) if timestamps else None
    if newest is None:
        # Geofabrik being down is not ours to escalate: the data we already
        # hold stays valid. Callers treat None as "nothing new to know".
        logger.error("No Geofabrik timestamp could be fetched; keeping current data")
    GEOFABRIK_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOFABRIK_TS_PATH.write_text(newest.isoformat() if newest else "")
    return newest


def probe_osm_freshness() -> None:
    """Fetch the Geofabrik timestamp, outside the network lock.

    The probe is a few hundred bytes of state.txt, but a slow Geofabrik makes
    it retry for minutes — and the network lock is there for the multi-GB PBF,
    not for a backoff sleep. Held here, it delayed the ATP and NSI downloads by
    as long as Geofabrik took to answer. Never raises: an unreachable source is
    reported by download_pbf, which owns that decision.
    """
    newest_geofabrik_timestamp()


def download_pbf() -> None:
    newest_ts = newest_geofabrik_timestamp()
    if newest_ts is None:
        raise SourceUnavailableError("Geofabrik")

    conn = connect()
    try:
        last_date = last_import_date(conn, "osm")
        start_import(conn, "osm")

        if (
            last_date
            and last_date >= newest_ts
            # The tables must also have been written by the code running now.
            and _matview.is_current(conn, "points", app_version())
        ):
            logger.info(
                "OSM data already up-to-date (last import: %s), skipping download",
                last_date.date(),
            )
            record_import(conn, "osm", last_date, "skipped")
            return
        if last_date and last_date >= newest_ts:
            logger.info("New revision since the last import, reimporting")
    finally:
        conn.close()

    logger.info("New OSM data available (newest: %s), downloading all regions...", newest_ts.date())
    for name, region in GEOFABRIK_REGIONS.items():
        pbf_path = region["pbf_path"]
        if pbf_path.exists():
            logger.info("PBF %s already present, skipping", name)
            continue
        logger.info("Downloading %s...", name)
        pbf_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            download_large_file(region["url"], pbf_path, session=_session)
        except Exception:
            delete_file_if_exists(pbf_path)
            raise
        logger.info("Downloaded %s", name)


def _require_free_space(path: Path, needed_bytes: float) -> None:
    """Fast-fail if the filesystem holding `path` has less than `needed_bytes` free.

    The import writes beside the live tables, which stay for its duration, so
    running out of disk halfway costs nothing but the hours it took — still
    worth a clear message before rather than an osm2pgsql exit code after.
    """
    free = shutil.disk_usage(path).free
    if free < needed_bytes:
        raise RuntimeError(
            f"Not enough free disk for osm2pgsql: {free / 1e9:.1f} GB free at "
            f"{path}, need ~{needed_bytes / 1e9:.1f} GB. Free space and retry "
            f"(`python -m src.pipeline from osm-import`)."
        )


def build_subdivision_parts() -> None:
    """Cut the boundaries into index-sized pieces for the POI attachment.

    An administrative boundary is a huge polygon: 140 of them carry 3.9M
    vertices in France, up to 255k on a single one. The GIST index narrows a
    point to two or three candidates, but the containment recheck that follows
    then walks a quarter of a million vertices, once per POI — and the
    per-POI loop throws away the prepared geometry PostGIS would otherwise
    cache. Measured on 5000 points: 33 minutes extrapolated to a full ATP
    import, against 17 seconds on the pieces, for identical results.

    ST_Intersects, not ST_Contains: the pieces share their cut lines, and a
    point landing exactly on one must not fall through to the coarser level.
    It also stops dropping a POI sitting exactly on a real border, which is an
    improvement — the ORDER BY still makes the answer deterministic.
    """
    conn = connect()
    try:
        # A reimport swaps a new subdivisions table in, so its oid identifies
        # the data these pieces were cut from, with nothing to record on the
        # side. Cutting takes about a minute, too much to
        # repeat nightly for a table that has not moved.
        with conn.cursor() as cur:
            found = cur.execute("SELECT to_regclass('subdivisions')::oid").fetchone()
        oid: int | None = found[0] if found else None
        if oid is None:
            # Unreachable now that the download is gated on the revision: a
            # deploy that starts writing subdivisions reimports on its own.
            # Kept as an assertion, on the branch that owns the table rather
            # than three steps later in the ATP import.
            raise RuntimeError(
                "No subdivisions table: generic.lua did not write one on the last import."
            )
        signature = _matview.signature(app_version(), oid)
        if _matview.is_current(conn, "subdivision_parts", signature):
            logger.info("Subdivision pieces already up-to-date, skipping")
            return

        with conn.cursor() as cur:
            logger.info("Cutting subdivisions into index-sized pieces...")
            cur.execute("DROP TABLE IF EXISTS subdivision_parts")
            cur.execute("""
                CREATE TABLE subdivision_parts AS
                SELECT osm_id, ref, name, admin_level, ST_Subdivide(geom, 256) AS geom
                  FROM subdivisions
            """)
            cur.execute("""
                CREATE INDEX subdivision_parts_geom_idx
                    ON subdivision_parts USING GIST (geom);
                CREATE INDEX subdivision_parts_admin_level_idx
                    ON subdivision_parts (admin_level);
            """)
            counted = cur.execute("SELECT count(*) FROM subdivision_parts").fetchone()
            _matview.stamp(cur, "subdivision_parts", signature, "TABLE")
        conn.commit()
        logger.info("%d subdivision piece(s) ready", counted[0] if counted else 0)
    finally:
        conn.close()


# Where osm2pgsql writes: a schema of its own, beside the live tables, since
# --create drops and recreates whatever it finds under the names it is given.
# The tables move to public once the import is complete.
IMPORT_SCHEMA = "osm_import"
OSM_TABLES = ("points", "polygons", "subdivisions")


def _import_pbfs() -> None:
    # All-or-nothing: osm2pgsql runs with --create, which starts the tables
    # from scratch. Importing a subset would silently replace the whole
    # planet extract with whatever leftovers a previous failed run left behind.
    pbf_paths = [r["pbf_path"] for r in GEOFABRIK_REGIONS.values()]
    missing = [p for p in pbf_paths if not p.exists()]
    if len(missing) == len(pbf_paths):
        logger.info("No PBF files found, skipping osm2pgsql")
        return
    if missing:
        raise RuntimeError(
            "Refusing a partial osm2pgsql --create: missing " + ", ".join(p.name for p in missing)
        )

    # Fast-fail on low disk before hours of import.
    # Heuristic: need ~3x total PBF size (tables + indexes + temp), floor 15 GB.
    # Override the floor with OSM2PGSQL_MIN_FREE_GB.
    total_pbf = sum(p.stat().st_size for p in pbf_paths)
    floor = get_pipeline().min_free_gb * 1e9
    needed = max(floor, 3 * total_pbf)
    _require_free_space(pbf_paths[0].parent, needed)

    db = get_database()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(IMPORT_SCHEMA))
            )
            cur.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(IMPORT_SCHEMA)))
        conn.commit()
    finally:
        conn.close()

    logger.info("Importing %d PBF file(s) into PostGIS...", len(pbf_paths))
    env = os.environ.copy()
    env["PGPASSWORD"] = db.password
    # generic.lua reads them: the Lua style has no access to the configuration.
    env["ATP2OSM_ADMIN_LEVEL_MAX"] = str(ADMIN_LEVEL_MAX)
    env["ATP2OSM_IMPORT_SCHEMA"] = IMPORT_SCHEMA
    # A fixed command line, no shell; osm2pgsql is on the image's PATH.
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "osm2pgsql",
            "--output",
            "flex",
            # 2.x reads version/timestamp on its own, 1.x only with -x. Kept
            # for the latter: without them the freshness guard has nothing to
            # filter on.
            "-x",
            "-S",
            str(PROJECT_ROOT / "osm2pgsql" / "generic.lua"),
            "-d",
            db.name,
            "-U",
            db.user,
            "-H",
            db.host,
            "-P",
            db.port,
            *[str(p) for p in pbf_paths],
        ],
        check=True,
        env=env,
    )

    # One transaction: the three tables move together, and the live ones
    # retire under another name rather than being dropped — mv_places is
    # materialized on them, and goes on serving until osm-views swaps it. The
    # retired tables go once mv-brand has swapped the brand view too.
    conn = connect()
    try:
        with conn.cursor() as cur:
            for table in OSM_TABLES:
                _matview.swap(cur, "TABLE", table, f"{IMPORT_SCHEMA}.{table}")
            _matview.stamp(cur, "points", app_version(), "TABLE")
            cur.execute(sql.SQL("DROP SCHEMA {}").format(sql.Identifier(IMPORT_SCHEMA)))
        conn.commit()
    finally:
        conn.close()

    # Only once the tables are in: a swap that failed can be retried from
    # here without downloading again.
    for p in pbf_paths:
        p.unlink()

    logger.info("osm2pgsql import complete (%d file(s))", len(pbf_paths))


def run_osm2pgsql() -> None:
    _import_pbfs()
    # Outside _import_pbfs on purpose: it returns early when Geofabrik has
    # published nothing, and the pieces still have to exist — they are derived
    # from our code, not from the data. Production found that out the hard way,
    # the step reporting success while the ATP import then failed on a table
    # that had never been built.
    build_subdivision_parts()


def mv_places_sql(name: str = "mv_places") -> str:
    # Only rows that can ever match are kept: the join in
    # MATCHED_POI_SQL requires an equality on one of brand:wikidata,
    # brand, name, email, website or phone, and NULL never equals
    # anything. That drops 95% of the OSM objects (20.3M -> 1.1M) and
    # cuts the /validate query time by a third to two thirds, with a
    # provably identical result.
    matchable = """
        WHERE tags ?| ARRAY['name', 'brand', 'brand:wikidata',
                            'email', 'contact:email',
                            'phone', 'contact:phone',
                            'website', 'contact:website']
    """
    # The matchable filter sits in a subquery so that the NSI
    # lookup only ever runs on the 1.1M rows that survive it,
    # never on the 20.3M raw ones.
    return f"""
        CREATE MATERIALIZED VIEW {name} AS
        SELECT
            node_id                                              AS osm_id,
            'node'                                               AS node_type,
            points.tags                                          AS tags,
            points.tags->>'name'                                 AS name,
            COALESCE(points.tags->>'brand:wikidata',
                     nsi.tags->>'brand:wikidata')        AS brand_wikidata,
            CASE WHEN points.tags ? 'brand:wikidata' THEN 'osm'
                 WHEN nsi.tags IS NOT NULL          THEN 'nsi'
            END                                                  AS brand_wikidata_source,
            nsi.tags                                             AS nsi_tags,
            points.tags->>'brand'                                AS brand,
            points.tags->>'addr:city'                            AS city,
            points.tags->>'addr:postcode'                        AS postcode,
            points.tags->>'opening_hours'                        AS opening_hours,
            COALESCE(points.tags->>'website', points.tags->>'contact:website') AS website,
            COALESCE(points.tags->>'phone', points.tags->>'contact:phone')     AS phone,
            COALESCE(points.tags->>'email', points.tags->>'contact:email')     AS email,
            version,
            to_timestamp(points.osm_timestamp)                    AS osm_timestamp,
            NULL::jsonb                                          AS members,
            geom
        FROM (SELECT * FROM points {matchable}) points
        LEFT JOIN LATERAL nsi_match(points.tags) AS nsi(tags) ON TRUE

        UNION ALL

        SELECT
            area_id                                              AS osm_id,
            CASE osm_type WHEN 'W' THEN 'way' ELSE 'relation' END AS node_type,
            polygons.tags                                        AS tags,
            polygons.tags->>'name'                               AS name,
            COALESCE(polygons.tags->>'brand:wikidata',
                     nsi.tags->>'brand:wikidata')        AS brand_wikidata,
            CASE WHEN polygons.tags ? 'brand:wikidata' THEN 'osm'
                 WHEN nsi.tags IS NOT NULL          THEN 'nsi'
            END                                                  AS brand_wikidata_source,
            nsi.tags                                             AS nsi_tags,
            polygons.tags->>'brand'                              AS brand,
            polygons.tags->>'addr:city'                          AS city,
            polygons.tags->>'addr:postcode'                      AS postcode,
            polygons.tags->>'opening_hours'                      AS opening_hours,
            COALESCE(polygons.tags->>'website', polygons.tags->>'contact:website') AS website,
            COALESCE(polygons.tags->>'phone', polygons.tags->>'contact:phone')     AS phone,
            COALESCE(polygons.tags->>'email', polygons.tags->>'contact:email')     AS email,
            version,
            to_timestamp(polygons.osm_timestamp)                  AS osm_timestamp,
            members                                              AS members,
            geom
        FROM (SELECT * FROM polygons {matchable}) polygons
        LEFT JOIN LATERAL nsi_match(polygons.tags) AS nsi(tags) ON TRUE
    """  # noqa: S608 — composed from code constants


# Canonical name to definition. Built under `<name>_new` on the new view and
# renamed with it: PHONE_INDEXES in src/phone.py names the phone one.
MV_PLACES_INDEXES: dict[str, LiteralString] = {
    "mv_places_geog_idx": "USING GIST ((geom::geography))",
    "mv_places_brand_wikidata_idx": "((brand_wikidata))",
    "mv_places_brand_lower_idx": "(LOWER(brand))",
    "mv_places_name_lower_idx": "(LOWER(name))",
    "mv_places_website_norm_idx": "(LOWER(REGEXP_REPLACE(website, '^https?://', '', 'i')))",
    "mv_places_phone_norm_idx": "(normalize_phone(phone))",
    "mv_places_email_lower_idx": "(LOWER(email))",
}


def setup_mv_places() -> None:
    newest_ts = newest_geofabrik_timestamp()
    conn = connect()
    try:
        last_date = last_import_date(conn, "osm")
        if newest_ts is None:
            # Geofabrik down: the OSM data cannot have moved under us, but the
            # NSI / function signature below may still require a rebuild.
            if last_date is None:
                logger.warning("Geofabrik unreachable and no prior import, skipping")
                return
            newest_ts = last_date
        # mv_places reads the OSM tables and nsi_brands: a new NSI release must
        # rebuild it even when the OSM data has not moved. NSI is identified by
        # its published version rather than a date — that is what names the
        # content, and two releases can share a day.
        #
        # The OSM date is not an input here: it is the check below. The
        # revision covers the rest — the view's own SQL, and nsi_match(),
        # whose body a migration redefines and a migration ships with a deploy.
        signature = _matview.signature(
            app_version(),
            last_import_comment(conn, "nsi"),
        )
        if (
            last_date
            and last_date >= newest_ts
            and _matview.is_current(conn, "mv_places", signature)
        ):
            logger.info("OSM views already up-to-date (%s), skipping", last_date.date())
            record_import(conn, "osm", last_date, "skipped")
            return

        try:
            # Built beside the live view and swapped in at the end, in one
            # transaction: the site reads the old rows for the minutes the
            # build takes, and a failure leaves them exactly as they were.
            # mv_places_brand is materialized on the old view, which
            # therefore retires under another name instead of being dropped:
            # it goes once mv-brand has swapped the brand view too.
            with conn.cursor() as cur:
                cur.execute("DROP MATERIALIZED VIEW IF EXISTS mv_places_new;")
                logger.info("Creating mv_places and indexes...")
                cur.execute(code_sql(mv_places_sql("mv_places_new")))
                _matview.create_indexes(cur, "mv_places_new", MV_PLACES_INDEXES)
                _matview.stamp(cur, "mv_places_new", signature)
                _matview.swap(
                    cur,
                    "MATERIALIZED VIEW",
                    "mv_places",
                    "mv_places_new",
                    tuple(MV_PLACES_INDEXES),
                )
            # Commits the swap with it.
            record_import(conn, "osm", newest_ts, "success")
            logger.info("mv_places created (data date: %s)", newest_ts.date())

        except Exception:
            logger.exception("setup_mv_places failed")
            raise
    finally:
        conn.close()
