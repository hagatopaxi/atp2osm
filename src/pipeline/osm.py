import logging
import os
import shutil
import subprocess
from datetime import datetime
from email.utils import parsedate_to_datetime
from src.pipeline import _matview
from src.pipeline._version import app_version
from src.pipeline.errors import SourceUnavailable
from src.pipeline.constants import (
    ADMIN_LEVEL_MAX,
    PROJECT_ROOT,
    GEOFABRIK_REGIONS,
    GEOFABRIK_TS_PATH,
)

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import get_database, get_pipeline
from src.pipeline._db import (
    connect,
    last_import_comment,
    last_import_date,
    record_import,
    start_import,
)
from src.utils import delete_file_if_exists, download_large_file

logger = logging.getLogger(__name__)

# Geofabrik hands out 502/503 for a few minutes at a time. Retry with backoff
# instead of failing the whole nightly run on a transient blip.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(max_retries=Retry(
    total=5,
    backoff_factor=5,          # waits 0, 5, 10, 20, 40s
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "HEAD"),
)))


def _geofabrik_timestamp(region: dict) -> datetime:
    """Fetch the data timestamp for a region.

    Tries the Geofabrik state.txt first; falls back to the HTTP Last-Modified
    header of the PBF file for regions that don't publish a state file.
    """
    try:
        resp = _session.get(region["state_url"], timeout=30)
        resp.raise_for_status()
        for line in resp.text.splitlines():
            if line.startswith("timestamp="):
                ts = line[len("timestamp="):].replace("\\:", ":")
                return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        pass

    # Fallback: Last-Modified header on the PBF file
    resp = _session.head(region["url"], timeout=30, allow_redirects=True)
    resp.raise_for_status()
    last_modified = resp.headers.get("Last-Modified")
    if last_modified:
        return parsedate_to_datetime(last_modified)

    raise ValueError(f"Cannot determine data timestamp for {region['url']}")


def forget_geofabrik_timestamp():
    """Drop what a previous run left behind. Called at the start of every run."""
    delete_file_if_exists(GEOFABRIK_TS_PATH)


def _newest_geofabrik_timestamp() -> datetime | None:
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

    timestamps = []
    for name, region in GEOFABRIK_REGIONS.items():
        try:
            timestamps.append(_geofabrik_timestamp(region))
        except Exception as exc:
            logger.error("Could not fetch timestamp for %s: %s", name, exc)
    newest = max(timestamps) if timestamps else None
    if newest is None:
        # Geofabrik being down is not ours to escalate: the data we already
        # hold stays valid. Callers treat None as "nothing new to know".
        logger.error("No Geofabrik timestamp could be fetched; keeping current data")
    GEOFABRIK_TS_PATH.parent.mkdir(parents=True, exist_ok=True)
    GEOFABRIK_TS_PATH.write_text(newest.isoformat() if newest else "")
    return newest


def probe_osm_freshness():
    """Fetch the Geofabrik timestamp, outside the network lock.

    The probe is a few hundred bytes of state.txt, but a slow Geofabrik makes
    it retry for minutes — and the network lock is there for the multi-GB PBF,
    not for a backoff sleep. Held here, it delayed the ATP and NSI downloads by
    as long as Geofabrik took to answer. Never raises: an unreachable source is
    reported by download_pbf, which owns that decision.
    """
    _newest_geofabrik_timestamp()


def download_pbf():
    newest_ts = _newest_geofabrik_timestamp()
    if newest_ts is None:
        raise SourceUnavailable("Geofabrik")

    conn = connect()
    try:
        last_date = last_import_date(conn, "osm")
        start_import(conn, "osm")  # puts the site in maintenance mode

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
            download_large_file(region["url"], pbf_path)
        except Exception:
            delete_file_if_exists(pbf_path)
            raise
        logger.info("Downloaded %s", name)


def _require_free_space(path, needed_bytes):
    """Fast-fail if the filesystem holding `path` has less than `needed_bytes` free.

    osm2pgsql --create drops and recreates points/polygons with CASCADE, which
    also drops mv_places and mv_places_brand. If it then runs out of disk it
    exits non-zero with the views already gone — leaving the site broken. Bail
    out *before* that happens, with a clear message.
    """
    free = shutil.disk_usage(path).free
    if free < needed_bytes:
        raise RuntimeError(
            f"Not enough free disk for osm2pgsql: {free / 1e9:.1f} GB free at "
            f"{path}, need ~{needed_bytes / 1e9:.1f} GB. Free space and retry "
            f"(`python -m src.pipeline from osm-import`)."
        )


def _build_subdivision_parts():
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
        # osm2pgsql runs with --create, so a reimport gives subdivisions a new
        # oid: it identifies the data these pieces were cut from, with nothing
        # to record on the side. Cutting takes about a minute, too much to
        # repeat nightly for a table that has not moved.
        with conn.cursor() as cur:
            oid = cur.execute("SELECT to_regclass('subdivisions')::oid").fetchone()[0]
        if oid is None:
            # Unreachable now that the download is gated on the revision: a
            # deploy that starts writing subdivisions reimports on its own.
            # Kept as an assertion, on the branch that owns the table rather
            # than three steps later in the ATP import.
            raise RuntimeError(
                "No subdivisions table: generic.lua did not write one on the "
                "last import."
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
            count = cur.execute("SELECT count(*) FROM subdivision_parts").fetchone()[0]
            _matview.stamp(cur, "subdivision_parts", signature, "TABLE")
        conn.commit()
        logger.info("%d subdivision piece(s) ready", count)
    finally:
        conn.close()


def _import_pbfs():
    # All-or-nothing: osm2pgsql runs with --create, which drops and recreates
    # points/polygons. Importing a subset would silently replace the whole
    # planet extract with whatever leftovers a previous failed run left behind.
    pbf_paths = [r["pbf_path"] for r in GEOFABRIK_REGIONS.values()]
    missing = [p for p in pbf_paths if not p.exists()]
    if len(missing) == len(pbf_paths):
        logger.info("No PBF files found, skipping osm2pgsql")
        return
    if missing:
        raise RuntimeError(
            "Refusing a partial osm2pgsql --create: missing "
            + ", ".join(p.name for p in missing)
        )

    # Fast-fail on low disk before the destructive CASCADE-dropping import.
    # Heuristic: need ~3x total PBF size (tables + indexes + temp), floor 15 GB.
    # Override the floor with OSM2PGSQL_MIN_FREE_GB.
    total_pbf = sum(p.stat().st_size for p in pbf_paths)
    floor = get_pipeline().min_free_gb * 1e9
    needed = max(floor, 3 * total_pbf)
    _require_free_space(pbf_paths[0].parent, needed)

    db = get_database()
    logger.info("Importing %d PBF file(s) into PostGIS...", len(pbf_paths))
    env = os.environ.copy()
    env["PGPASSWORD"] = db.password
    # generic.lua reads it: the Lua style has no access to the configuration.
    env["ATP2OSM_ADMIN_LEVEL_MAX"] = str(ADMIN_LEVEL_MAX)
    subprocess.run(
        [
            "osm2pgsql",
            "--output", "flex",
            # 2.x reads version/timestamp on its own, 1.x only with -x. Kept
            # for the latter: without them the freshness guard has nothing to
            # filter on.
            "-x",
            "-S", str(PROJECT_ROOT / "osm2pgsql" / "generic.lua"),
            "-d", db.name,
            "-U", db.user,
            "-H", db.host,
            "-P", db.port,
            *[str(p) for p in pbf_paths],
        ],
        check=True,
        env=env,
    )

    for p in pbf_paths:
        p.unlink()

    conn = connect()
    try:
        with conn.cursor() as cur:
            _matview.stamp(cur, "points", app_version(), "TABLE")
        conn.commit()
    finally:
        conn.close()

    logger.info("osm2pgsql import complete (%d file(s))", len(pbf_paths))


def run_osm2pgsql():
    _import_pbfs()
    # Outside _import_pbfs on purpose: it returns early when Geofabrik has
    # published nothing, and the pieces still have to exist — they are derived
    # from our code, not from the data. Production found that out the hard way,
    # the step reporting success while the ATP import then failed on a table
    # that had never been built.
    _build_subdivision_parts()


def _mv_places_sql() -> str:
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
        CREATE MATERIALIZED VIEW mv_places AS
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
    """


def setup_mv_places():
    view_sql = _mv_places_sql()
    newest_ts = _newest_geofabrik_timestamp()
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
            with conn.cursor() as cur:
                cur.execute("DROP MATERIALIZED VIEW IF EXISTS mv_places CASCADE;")
                logger.info("Creating mv_places and indexes...")
                cur.execute(view_sql)

                cur.execute("""
                    CREATE INDEX IF NOT EXISTS mv_places_geog_idx
                        ON mv_places USING GIST ((geom::geography));
                    CREATE INDEX IF NOT EXISTS mv_places_brand_wikidata_idx
                        ON mv_places ((brand_wikidata));
                    CREATE INDEX IF NOT EXISTS mv_places_brand_lower_idx
                        ON mv_places (LOWER(brand));
                    CREATE INDEX IF NOT EXISTS mv_places_name_lower_idx
                        ON mv_places (LOWER(name));
                    CREATE INDEX IF NOT EXISTS mv_places_website_norm_idx
                        ON mv_places (LOWER(REGEXP_REPLACE(website, '^https?://', '', 'i')));
                    CREATE INDEX IF NOT EXISTS mv_places_phone_norm_idx
                        ON mv_places (normalize_phone(phone));
                    CREATE INDEX IF NOT EXISTS mv_places_email_lower_idx
                        ON mv_places (LOWER(email));
                """)

                _matview.stamp(cur, "mv_places", signature)

            conn.commit()
            record_import(conn, "osm", newest_ts, "success")
            logger.info("mv_places created (data date: %s)", newest_ts.date())

        except Exception:
            logger.exception("setup_mv_places failed")
            raise
    finally:
        conn.close()
