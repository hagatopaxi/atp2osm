import logging
from typing import Any, LiteralString

from psycopg import Cursor

from src.db import code_sql
from src.matching import matched_poi_sql, waves_lateral_sql
from src.pipeline import _matview
from src.pipeline._db import connect, last_import_comment, last_import_date
from src.pipeline._version import app_version

logger = logging.getLogger(__name__)


def mv_places_brand_sql(name: str = "mv_places_brand") -> str:
    # The wave flags are filtered AFTER deduplication, like apply_on_node()
    # does on the /validate side, otherwise the two counts diverge.
    # One row per (brand, subdivision, wave): get_all() sums the unblocked ones
    # of the brand's current wave to announce what is still left to integrate.
    # A POI counts on every wave it qualifies for — a missing phone and a stale
    # website are two integrations, done one after the other.
    return f"""
        CREATE MATERIALIZED VIEW {name} AS
        SELECT
            STRING_AGG(DISTINCT atp_brand, ' / ' ORDER BY atp_brand) AS brand,
            atp_brand_wikidata AS brand_wikidata,
            subdivision_code,
            w.wave             AS wave,
            COUNT(*)           AS total
        FROM ({matched_poi_sql("TRUE")}) matched
        CROSS JOIN LATERAL (VALUES {waves_lateral_sql()}) AS w(wave, matches)
        WHERE w.matches
        GROUP BY atp_brand_wikidata, subdivision_code, w.wave
    """  # noqa: S608 — composed from code constants


def mv_places_spider_sql(name: str = "mv_places_spider") -> str:
    # ATP POIs that met an OSM object, per spider — the /spiders page's
    # "matched" column. Counted before the wave flags: a match with nothing
    # left to integrate is still a match, that is what measures the deposit.
    return f"""
        CREATE MATERIALIZED VIEW {name} AS
        SELECT spider_id, COUNT(*) AS matched
        FROM ({matched_poi_sql("TRUE")}) matched
        GROUP BY spider_id
    """  # noqa: S608 — composed from code constants


# What the upstream steps retire, readers first: the old brand view hangs on
# mv_places_old, which hangs on points_old and polygons_old. Nothing hangs on
# the old brand view, so the whole chain frees up here, once it is swapped.
_RETIRED: tuple[tuple[LiteralString, str], ...] = (
    ("MATERIALIZED VIEW", "mv_places_brand"),
    ("MATERIALIZED VIEW", "mv_places_spider"),
    ("MATERIALIZED VIEW", "mv_places"),
    ("TABLE", "atp_places"),
    ("TABLE", "atp_spiders"),
    ("TABLE", "points"),
    ("TABLE", "polygons"),
    ("TABLE", "subdivisions"),
)


def _drop_retired(cur: Cursor[Any]) -> None:
    for kind, name in _RETIRED:
        for dropped in _matview.drop_retired(cur, kind, name):
            logger.info("Dropped retired %s", dropped)


def create_mv_places_brand() -> None:
    conn = connect()
    try:
        # It counts matches between mv_places and atp_places, so it has to be
        # rebuilt when either moves — and when MATCHED_POI_SQL itself changes,
        # since /validate applies that same SQL live and the two counts must
        # never diverge.
        # normalize_phone is an input too: the view's SQL calls it without
        # carrying its body, so changing the phone key would move the counts
        # while leaving the signature untouched — the list and /validate would
        # then disagree on how many POIs a brand has left.
        signature = _matview.signature(
            app_version(),
            last_import_date(conn, "osm"),
            last_import_date(conn, "atp"),
            last_import_comment(conn, "nsi"),  # the NSI version string
        )
        if _matview.is_current(conn, "mv_places_brand", signature) and (
            _matview.is_current(conn, "mv_places_spider", signature)
        ):
            logger.info("mv_places_brand already up-to-date, skipping")
            return

        # Built beside the live view, then swapped in: the site keeps reading
        # the old rows for the minutes the join takes, and only waits on the
        # exclusive lock for the swap at the very end. A DROP first would hold
        # that lock for the whole build — every request hanging on it. One
        # transaction: a failure leaves the old view exactly as it was.
        with conn.cursor() as cur:
            cur.execute("DROP MATERIALIZED VIEW IF EXISTS mv_places_brand_new;")
            logger.info("Creating mv_places_brand...")
            cur.execute(code_sql(mv_places_brand_sql("mv_places_brand_new")))
            _matview.stamp(cur, "mv_places_brand_new", signature)
            _matview.swap(cur, "MATERIALIZED VIEW", "mv_places_brand", "mv_places_brand_new")
            cur.execute("DROP MATERIALIZED VIEW IF EXISTS mv_places_spider_new;")
            logger.info("Creating mv_places_spider...")
            cur.execute(code_sql(mv_places_spider_sql("mv_places_spider_new")))
            _matview.stamp(cur, "mv_places_spider_new", signature)
            _matview.swap(cur, "MATERIALIZED VIEW", "mv_places_spider", "mv_places_spider_new")
            _drop_retired(cur)
        conn.commit()
        logger.info("mv_places_brand created")
    finally:
        conn.close()
