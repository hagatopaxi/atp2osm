import logging

from src.matching import matched_poi_sql
from src.pipeline import _matview
from src.pipeline._version import app_version
from src.pipeline._db import connect, last_import_comment, last_import_date

logger = logging.getLogger(__name__)


def _mv_places_brand_sql() -> str:
    # is_importable is filtered AFTER deduplication, like apply_on_node() does
    # on the /validate side, otherwise the two counts diverge.
    # One row per (brand, subdivision): get_all() sums the unblocked
    # ones to announce what is still left to integrate.
    return f"""
        CREATE MATERIALIZED VIEW mv_places_brand AS
        SELECT
            STRING_AGG(DISTINCT atp_brand, ' / ' ORDER BY atp_brand) AS brand,
            atp_brand_wikidata AS brand_wikidata,
            subdivision_code,
            COUNT(*)           AS total
        FROM ({matched_poi_sql("TRUE")}) matched
        WHERE is_importable
        GROUP BY atp_brand_wikidata, subdivision_code
    """


def create_mv_places_brand():
    view_sql = _mv_places_brand_sql()
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
        if _matview.is_current(conn, "mv_places_brand", signature):
            logger.info("mv_places_brand already up-to-date, skipping")
            return

        with conn.cursor() as cur:
            cur.execute("DROP MATERIALIZED VIEW IF EXISTS mv_places_brand;")
            logger.info("Creating mv_places_brand...")
            cur.execute(view_sql)
            _matview.stamp(cur, "mv_places_brand", signature)
        conn.commit()
        logger.info("mv_places_brand created")
    finally:
        conn.close()
