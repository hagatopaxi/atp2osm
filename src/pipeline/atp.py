import json
import logging
import shutil
import zipfile
from datetime import datetime, timezone
from src.pipeline.constants import (
    ADMIN_LEVEL,
    ATP_DIR,
    GEOJSON_DIR,
    SPLIT_DIR,
    PARQUET_PATH,
    SPIDERS_PATH,
    ATP_HISTORY_URL,
)
import duckdb
import requests

from src.config import get_country, get_database
from src.pipeline._version import app_version
from src.pipeline.errors import unavailable_if_unreachable
from src.pipeline.osm import forget_geofabrik_timestamp
from src.pipeline._db import (
    connect,
    last_import_comment,
    last_import_date,
    record_import,
    start_import,
)
from src.pipeline.ndgeojson_to_parquet import convert_to_parquet
from src.utils import delete_file_if_exists, download_large_file


logger = logging.getLogger(__name__)


# ISO 3166-1 alpha-2 codes. ATP names its country-specific spiders
# `<brand>_<cc>` (e.g. `aldi_de`), so a suffix that is a foreign code means no
# POI of ours. Only the suffix: a leading `la_`/`au_`/`as_` is part of the
# brand name much more often than it is a country (la_halle_fr,
# au_vieux_campeur, as_24_fr).
_COUNTRY_CODES = frozenset(
    """ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bl bm
    bn bo bq br bs bt bv bw by bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy
    cz de dj dk dm do dz ec ee eg eh er es et fi fj fk fm fo fr ga gb gd ge gf gg gh gi
    gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je
    jm jo jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md
    me mf mg mh mk ml mm mn mo mp mq mr ms mt mu mv mw mx my mz na nc ne nf ng ni nl no
    np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs ru rw sa sb sc
    sd se sg sh si sj sk sl sm sn so sr ss st sv sx sy sz tc td tf tg th tj tk tl tm tn
    to tr tt tv tw tz ua ug um us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw""".split()
)


def _foreign_country_codes() -> frozenset[str]:
    """Every country code but ours — computed, so a country lists nothing."""
    return _COUNTRY_CODES - {get_country().code}


def is_relevant_spider(filename: str) -> bool:
    """True unless the spider is foreign, or a bulk address dataset.

    National address datasets (au_vic_addresses, nz_addresses, …) hold no brand
    yet weigh 44% of ATP's POI: nothing to match, a lot to carry.
    """
    stem = filename.rsplit("/", 1)[-1].removesuffix(".geojson").lower()
    return (
        stem.rsplit("_", 1)[-1] not in _foreign_country_codes()
        and "addresses" not in stem
    )


def select_run(runs, last_date):
    """Newest ATP run worth downloading, or None if we already have it.

    `runs` comes newest-first. A run whose end_time is not strictly newer than
    the last recorded import means ATP published nothing since — the whole ATP
    branch then no-ops for the rest of the pipeline.
    """
    for run in runs:
        if not run.get("parquet_url"):
            continue
        end_time_raw = run.get("end_time")
        end_time = (
            datetime.fromisoformat(end_time_raw.replace("Z", "+00:00"))
            if end_time_raw
            else None
        )
        if last_date is not None and end_time is not None and end_time <= last_date:
            return None
        return run
    raise RuntimeError("No ATP run could be downloaded")


def download_atp():
    conn = connect()
    try:
        last_date = last_import_date(conn, "atp")
        start_import(conn, "atp")  # puts the site in maintenance mode

        with unavailable_if_unreachable("ATP"):
            resp = requests.get(ATP_HISTORY_URL, timeout=30)
            resp.raise_for_status()
            runs = list(reversed(resp.json()))

        ATP_DIR.mkdir(parents=True, exist_ok=True)

        run = select_run(runs, last_date)
        if run is None:
            logger.info("ATP already up-to-date, skipping")
            # Nothing downstream knows this step decided to stop: extract,
            # convert, split and parquet are each guarded by the presence of
            # the files the previous step wrote, not by that decision. A run
            # that failed further along leaves them behind — cleanup is the
            # last node of the DAG, so any failure upstream skips it — and the
            # whole branch then rebuilds a byte-identical parquet, whose fresh
            # mtime also costs import_atp its own no-op. Clearing the workdir
            # here makes every one of them no-op on the guard it already has.
            _discard_workdir()
            # last_date, not the run's end_time: recording an older run would
            # make the displayed source date go backwards. And the stamp is the
            # one already there, not app_version(): what it describes is the
            # atp_places sitting in the database, which this step did not rebuild.
            # Recording the running revision here would tell import_atp the
            # table is up to date when it is not.
            record_import(
                conn, "atp", last_date, "skipped", last_import_comment(conn, "atp")
            )
            return

        delete_file_if_exists(ATP_DIR / "output.zip")
        delete_file_if_exists(SPIDERS_PATH)

        with unavailable_if_unreachable("ATP"):
            download_large_file(run["output_url"], ATP_DIR / "output.zip")

        stats_url = run.get("stats_url")
        if stats_url:
            stats_path = ATP_DIR / "stats.json"
            with unavailable_if_unreachable("ATP"):
                download_large_file(stats_url, stats_path)
            with open(stats_path) as infile, open(SPIDERS_PATH, "w") as out:
                out.write(json.dumps(json.loads(infile.read())["results"]))
            stats_path.unlink()

        logger.info("Downloaded ATP run %s", run.get("run_id"))

    finally:
        conn.close()


def extract_atp():
    zip_path = ATP_DIR / "output.zip"
    if not zip_path.exists():
        logger.info("No ATP zip found, skipping extraction")
        return

    if GEOJSON_DIR.exists():
        shutil.rmtree(GEOJSON_DIR)
    GEOJSON_DIR.mkdir(parents=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [n for n in zf.namelist() if is_relevant_spider(n)]
        zf.extractall(GEOJSON_DIR, members)

    geojson_files = list(GEOJSON_DIR.rglob("*.geojson"))
    if not geojson_files:
        raise FileNotFoundError(f"No .geojson files found in {GEOJSON_DIR}")

    for f in geojson_files:
        if f.parent != GEOJSON_DIR:
            f.rename(GEOJSON_DIR / f.name)

    logger.info("Extracted ATP zip (%d geojson files)", len(geojson_files))


def create_parquet_atp():
    """Step 5: Create parquet from split NDJSON files."""
    if not SPLIT_DIR.exists() or not any(SPLIT_DIR.glob("*.geojson")):
        logger.info("No split NDJSON files found, skipping parquet creation")
        return
    delete_file_if_exists(PARQUET_PATH)
    convert_to_parquet(SPLIT_DIR, PARQUET_PATH)
    logger.info("Created parquet from NDJSON files")


def _attach_subdivisions(conn):
    """Attach every POI to the administrative subdivision that contains it.

    Replaces the derivation from the postcode, which only ever worked in
    France. Levels are walked from ADMIN_LEVEL down to the country itself, so a
    territory with no polygon at the finest level (Martinique and Guyane have no
    admin_level 6, French Polynesia stops at 3) still lands somewhere instead of
    vanishing.

    Level 2 is a country, so failing to attach means one thing only: the POI
    falls outside every country in the extracts. That replaces the
    postcode-shaped regex, and catches more — a well-formed postcode with wrong
    coordinates used to sail through.

    ponytail: a Geofabrik extract carries the neighbours' national boundaries
    too, so a POI can attach to Monaco or Andorra. They are not catching
    orphans — a POI no subdivision covers falls back on the country itself, and
    one outside every boundary is dropped above. What lands there is what is
    physically there: 86 POIs, 83 of them with a Monaco postcode, that ATP tags
    addr:country=FR. Kept, and integrated under a readable name. Filter on the
    country code the day an instance objects to it.
    """
    logger.info("Attaching POIs to subdivisions (admin_level <= %d)...", ADMIN_LEVEL)
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE atp_places ADD COLUMN subdivision_code TEXT,
                               ADD COLUMN subdivision_name TEXT;
        """)
        cur.execute(
            """
            UPDATE atp_places atp
               SET (subdivision_code, subdivision_name) = (
                    -- ponytail: ref is not unique across levels — 16 codes in
                    -- France name both a région and a département (75 is Paris
                    -- and Nouvelle-Aquitaine, 93 is Seine-Saint-Denis and PACA).
                    -- Harmless as long as no POI attaches at the coarser level,
                    -- and a région being the union of its départements, a point
                    -- inside one is inside the other. Qualify the code by its
                    -- level the day a collision actually shows up.
                    SELECT COALESCE(sub.ref, sub.osm_id::text), sub.name
                      FROM subdivision_parts sub
                     WHERE sub.admin_level <= %s
                       AND ST_Intersects(sub.geom, ST_GeomFromGeoJSON(atp.geom))
                     -- Finest level wins; osm_id only breaks a tie that should
                     -- not happen, so the result never depends on the physical
                     -- order of the rows.
                     ORDER BY sub.admin_level DESC, sub.osm_id
                     LIMIT 1
                   );
            """,
            (ADMIN_LEVEL,),
        )
        cur.execute("DELETE FROM atp_places WHERE subdivision_code IS NULL")
        dropped = cur.rowcount
    conn.commit()
    if dropped:
        logger.info("Dropped %d POI(s) falling outside the country", dropped)


def import_atp():
    conn = connect()
    try:
        if not PARQUET_PATH.exists():
            raise FileNotFoundError(
                f"No parquet file at {PARQUET_PATH} — atp-parquet must run first"
            )

        parquet_mtime = datetime.fromtimestamp(
            PARQUET_PATH.stat().st_mtime, tz=timezone.utc
        )
        last_date = last_import_date(conn, "atp")
        version = app_version()

        if (
            last_date is not None
            and parquet_mtime <= last_date
            and last_import_comment(conn, "atp") == version
        ):
            # download_atp already closed the row it opened with 'skipped':
            # recording here too would add a second row for the same run.
            logger.info(
                "Parquet not newer than last import (%s), skipping", last_date.date()
            )
            return

        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS atp_places CASCADE")
                cur.execute("DROP TABLE IF EXISTS atp_spiders CASCADE")
            conn.commit()

            db = get_database()
            db_url = (
                f"dbname={db.name} "
                f"user={db.user} "
                f"host={db.host} "
                f"password={db.password} "
                f"port={db.port}"
            )
            ddb = duckdb.connect()
            ddb.execute("INSTALL postgres; LOAD postgres;")
            ddb.execute("INSTALL spatial; LOAD spatial;")
            ddb.execute(f"ATTACH '{db_url}' AS pg (TYPE postgres);")

            logger.info("Creating atp_places table from parquet...")
            country_code = get_country().code.upper()
            ddb.execute(f"""
                CREATE TABLE pg.atp_places AS
                SELECT
                    id,
                    properties->>'$.addr:country'    AS country,
                    properties->>'$.addr:city'        AS city,
                    properties->>'$.addr:postcode'    AS postcode,
                    properties->>'$.brand:wikidata'   AS brand_wikidata,
                    properties->>'$.brand'            AS brand,
                    properties->>'$.name'             AS name,
                    properties->>'$.opening_hours'    AS opening_hours,
                    properties->>'$.website'          AS website,
                    properties->>'$.phone'            AS phone,
                    LOWER(properties->>'$.email')     AS email,
                    properties->>'$.end_date'         AS end_date,
                    properties->>'$.@spider'          AS spider_id,
                    NULL::VARCHAR                     AS source_type,
                    properties->>'$.@source_uri'      AS source_uri,
                    ST_AsGeoJSON(geom)                AS geom
                FROM read_parquet('{PARQUET_PATH}')
                WHERE properties->>'$.addr:country' = '{country_code}'
                    AND geom IS NOT NULL
            """)

            _attach_subdivisions(conn)

            logger.info("Creating indexes for atp_places...")
            with conn.cursor() as cur:
                cur.execute("""
                    CREATE INDEX IF NOT EXISTS atp_places_geom_idx
                        ON atp_places USING GIST ((ST_GeomFromGeoJSON(geom)::geography));
                    CREATE INDEX IF NOT EXISTS atp_places_brand_wikidata_idx
                        ON atp_places (brand_wikidata);
                    CREATE INDEX IF NOT EXISTS atp_places_brand_lower_idx
                        ON atp_places (LOWER(brand));
                    CREATE INDEX IF NOT EXISTS atp_places_name_lower_idx
                        ON atp_places (LOWER(name));
                    CREATE INDEX IF NOT EXISTS atp_places_website_norm_idx
                        ON atp_places (LOWER(REGEXP_REPLACE(website, '^https?://', '', 'i')));
                    CREATE INDEX IF NOT EXISTS atp_places_phone_norm_idx
                        ON atp_places (normalize_phone(phone));
                    CREATE INDEX IF NOT EXISTS atp_places_email_lower_idx
                        ON atp_places (LOWER(email));
                    CREATE INDEX IF NOT EXISTS atp_places_subdivision_code_idx
                        ON atp_places (subdivision_code);
                    CREATE INDEX IF NOT EXISTS atp_places_spider_idx
                        ON atp_places (spider_id);
                    CREATE INDEX IF NOT EXISTS atp_places_source_type_idx
                        ON atp_places (source_type);
                """)
            conn.commit()

            logger.info("Creating atp_spiders table...")
            ddb.execute(f"""
                CREATE TABLE pg.atp_spiders AS
                SELECT *
                FROM read_json('{SPIDERS_PATH}')
                WHERE spider IN (SELECT DISTINCT spider_id FROM pg.atp_places)
            """)

            record_import(conn, "atp", parquet_mtime, "success", version)
            logger.info("ATP import complete (parquet mtime: %s)", parquet_mtime.date())

        except Exception:
            logger.exception("import_atp failed")
            raise

    finally:
        conn.close()


def _discard_workdir():
    """Erase what a run downloaded and derived, keeping latest.parquet.

    The parquet is deliberately kept: it is what lets import_atp no-op on a run
    where ATP published nothing new.
    """
    for name in ["output.zip", "geojson", "ndgeojson", "split", "stats.json"]:
        path = ATP_DIR / name
        if not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        logger.info("Cleaned up %s", path)


def cleanup_atp():
    forget_geofabrik_timestamp()
    _discard_workdir()
