import json
import logging
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from src.pipeline.constants import (
    ADMIN_LEVEL,
    ATP_DIR,
    GEOJSON_DIR,
    SPLIT_DIR,
    PARQUET_PATH,
    SPIDERS_PATH,
    ATP_HISTORY_URL,
    ATP_REPO_DIR,
    ATP_REPO_URL,
)
import duckdb
import requests
from psycopg import sql

from src.config import get_country, get_database
from src.pipeline import _matview
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
    """Every country code but ours and its territories' — nothing to configure."""
    return _COUNTRY_CODES - set(get_country().territory_codes)


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


def spider_dates(repo: Path = ATP_REPO_DIR) -> dict[str, str]:
    """Date of the last commit touching each spider file, keyed on its path.

    A blobless clone: 20 MB for the whole history, and one walk over it gives
    every file at once. --no-renames matters — rename detection reads the blobs,
    and each one would be fetched on demand, one round-trip at a time.
    """
    if (repo / ".git").exists():
        subprocess.run(["git", "-C", str(repo), "fetch", "--quiet"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "reset", "--quiet", "--soft", "origin/HEAD"],
            check=True,
        )
    else:
        subprocess.run(
            ["git", "clone", "--quiet", "--filter=blob:none", "--no-checkout",
             ATP_REPO_URL, str(repo)],
            check=True,
        )
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-renames", "--format=%cI",
         "--name-only", "--", "locations/spiders"],
        check=True, capture_output=True, text=True,
    ).stdout
    return _parse_dated_log(log)


def _parse_dated_log(log: str) -> dict[str, str]:
    """`git log --format=%cI --name-only` output: a date, then its files."""
    dates: dict[str, str] = {}
    date = None
    for line in log.splitlines():
        if not line:
            continue
        if line.startswith("locations/"):
            dates.setdefault(line, date)  # newest first: the first one wins
        else:
            date = line
    return dates


def load_spiders(cur, path: Path, table: str) -> None:
    """Build `table` from spiders.json, on a declared schema.

    The file is ATP's stats.json plus the `updated_at` the download step adds:
    inferring the columns from it gave the site a column that was missing,
    JSON or VARCHAR depending on the file of the day. A column the site reads
    from here needs a bridging migration too (AGENTS.md, "Pipeline tables and
    the site"): production keeps the old table until this step runs again.
    """
    with open(path) as infile:
        spiders = json.load(infile)
    cur.execute(
        sql.SQL("""
            CREATE TABLE {} (
                spider TEXT PRIMARY KEY,
                filename TEXT,
                errors INT8,
                features INT8,
                elapsed_time FLOAT8,
                updated_at TIMESTAMPTZ
            )
        """).format(sql.Identifier(table))
    )
    cur.executemany(
        sql.SQL("INSERT INTO {} VALUES (%s, %s, %s, %s, %s, %s)").format(
            sql.Identifier(table)
        ),
        [
            (s["spider"], s.get("filename"), s.get("errors"), s.get("features"),
             s.get("elapsed_time"), s.get("updated_at"))
            for s in spiders
        ],
    )


def download_atp():
    conn = connect()
    try:
        last_date = last_import_date(conn, "atp")
        start_import(conn, "atp")

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
            with open(stats_path) as infile:
                spiders = json.load(infile)["results"]
            stats_path.unlink()
            # ponytail: GitHub down costs the dates of this run, not the run.
            try:
                dates = spider_dates()
            except subprocess.CalledProcessError:
                logger.exception("Could not date the spiders, leaving them undated")
                dates = {}
            for spider in spiders:
                spider["updated_at"] = dates.get(spider["filename"])
            with open(SPIDERS_PATH, "w") as out:
                json.dump(spiders, out)

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


def _attach_subdivisions(conn, table: str = "atp_places"):
    """Attach every POI in `table` to the administrative subdivision that
    contains it.

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
        cur.execute(
            sql.SQL(
                "ALTER TABLE {} ADD COLUMN subdivision_code TEXT,"
                " ADD COLUMN subdivision_name TEXT"
            ).format(sql.Identifier(table))
        )
        cur.execute(
            sql.SQL("""
            UPDATE {} atp
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
            """).format(sql.Identifier(table)),
            (ADMIN_LEVEL,),
        )
        cur.execute(
            sql.SQL("DELETE FROM {} WHERE subdivision_code IS NULL").format(
                sql.Identifier(table)
            )
        )
        dropped = cur.rowcount
    conn.commit()
    if dropped:
        logger.info("Dropped %d POI(s) falling outside the country", dropped)


# Canonical name to definition. Built under `<name>_new` on the new table and
# renamed with it: PHONE_INDEXES in src/phone.py names the phone one.
ATP_PLACES_INDEXES = {
    "atp_places_geom_idx": "USING GIST ((ST_GeomFromGeoJSON(geom)::geography))",
    "atp_places_brand_wikidata_idx": "(brand_wikidata)",
    "atp_places_brand_lower_idx": "(LOWER(brand))",
    "atp_places_name_lower_idx": "(LOWER(name))",
    "atp_places_website_norm_idx": "(LOWER(REGEXP_REPLACE(website, '^https?://', '', 'i')))",
    "atp_places_phone_norm_idx": "(normalize_phone(phone))",
    "atp_places_email_lower_idx": "(LOWER(email))",
    "atp_places_subdivision_code_idx": "(subdivision_code)",
    "atp_places_spider_idx": "(spider_id)",
    "atp_places_source_type_idx": "(source_type)",
}


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
            # Loaded beside the live tables and swapped in at the end: the
            # site reads the old rows for the minutes the load takes, and a
            # failure leaves them as they were. The old ones retire under
            # another name — mv_places_brand is materialized on atp_places —
            # and go once mv-brand has swapped the brand view too.
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS atp_places_new")
                cur.execute("DROP TABLE IF EXISTS atp_spiders_new")
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
            # A POI of ours carries the country code or one of its territories':
            # ISO codes Martinique MQ, and ATP follows its sources.
            countries = ", ".join(f"'{c.upper()}'" for c in get_country().territory_codes)
            ddb.execute(f"""
                CREATE TABLE pg.atp_places_new AS
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
                    -- The POI's primary tag, in the shape osm_primary_tag()
                    -- returns for an OSM object, and in the same order of
                    -- preference: MATCHED_POI_SQL compares the two before
                    -- trusting an equality of names. 96% of the POIs carry
                    -- one; the others get NULL, which the join tolerates.
                    list_filter([
                        ['shop',       properties->>'$.shop'],
                        ['amenity',    properties->>'$.amenity'],
                        ['tourism',    properties->>'$.tourism'],
                        ['office',     properties->>'$.office'],
                        ['leisure',    properties->>'$.leisure'],
                        ['healthcare', properties->>'$.healthcare'],
                        ['craft',      properties->>'$.craft'],
                        ['landuse',    properties->>'$.landuse']
                    ], t -> t[2] IS NOT NULL)[1]      AS category,
                    ST_AsGeoJSON(geom)                AS geom
                FROM read_parquet('{PARQUET_PATH}')
                WHERE properties->>'$.addr:country' IN ({countries})
                    AND geom IS NOT NULL
            """)

            _attach_subdivisions(conn, "atp_places_new")

            logger.info("Creating indexes for atp_places...")
            with conn.cursor() as cur:
                _matview.create_indexes(cur, "atp_places_new", ATP_PLACES_INDEXES)
            conn.commit()

            logger.info("Creating atp_spiders table...")
            with conn.cursor() as cur:
                load_spiders(cur, SPIDERS_PATH, "atp_spiders_new")
                cur.execute("""
                    DELETE FROM atp_spiders_new
                    WHERE spider NOT IN (SELECT DISTINCT spider_id FROM atp_places_new)
                """)
                _matview.swap(
                    cur, "TABLE", "atp_places", "atp_places_new", ATP_PLACES_INDEXES
                )
                _matview.swap(cur, "TABLE", "atp_spiders", "atp_spiders_new")
            # Commits the swap with it.
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
