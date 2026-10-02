import logging
from typing import Any, LiteralString

from psycopg import Cursor, sql

from src.matching import matched_poi_sql, waves_lateral_sql
from src.pipeline import _matview
from src.pipeline._db import connect, last_import_comment, last_import_date
from src.pipeline._version import app_version

logger = logging.getLogger(__name__)


# What /validate looks a brand up by.
MATCHES_INDEXES: dict[str, LiteralString] = {"mv_matches_brand_idx": "(atp_brand_wikidata)"}


def mv_matches_sql(name: str = "mv_matches") -> sql.Composed:
    # The join runs here, once a refresh, and nowhere else: /validate reads
    # its brand's rows, the two counters below aggregate them.
    return sql.SQL("CREATE MATERIALIZED VIEW {name} AS {matched}").format(
        name=sql.Identifier(name), matched=matched_poi_sql()
    )


def mv_places_brand_sql(name: str = "mv_places_brand", matches: str = "mv_matches") -> sql.Composed:
    # The wave flags are filtered AFTER deduplication, like apply_on_node()
    # does on the /validate side, otherwise the two counts diverge.
    # One row per (brand, subdivision, wave): get_all() sums the unblocked ones
    # of the brand's current wave to announce what is still left to integrate.
    # A POI counts on every wave it qualifies for — a missing phone and a stale
    # website are two integrations, done one after the other.
    return sql.SQL("""
        CREATE MATERIALIZED VIEW {name} AS
        SELECT
            STRING_AGG(DISTINCT brand, ' / ' ORDER BY brand) AS brand,
            atp_brand_wikidata AS brand_wikidata,
            subdivision_code,
            w.wave             AS wave,
            COUNT(*)           AS total
        FROM {matches} matched
        CROSS JOIN LATERAL (VALUES {waves}) AS w(wave, matches)
        WHERE w.matches
        GROUP BY atp_brand_wikidata, subdivision_code, w.wave
    """).format(
        name=sql.Identifier(name), matches=sql.Identifier(matches), waves=waves_lateral_sql()
    )


def mv_places_spider_sql(
    name: str = "mv_places_spider", matches: str = "mv_matches"
) -> sql.Composed:
    # ATP POIs that met an OSM object, per spider — the /spiders page's
    # "matched" column. Counted before the wave flags: a match with nothing
    # left to integrate is still a match, that is what measures the deposit.
    return sql.SQL("""
        CREATE MATERIALIZED VIEW {name} AS
        SELECT spider_id, COUNT(*) AS matched
        FROM {matches} matched
        GROUP BY spider_id
    """).format(name=sql.Identifier(name), matches=sql.Identifier(matches))


# What the upstream steps retire, readers first: the old brand view hangs on
# mv_places_old, which hangs on points_old and polygons_old. Nothing hangs on
# the old brand view, so the whole chain frees up here, once it is swapped.
_RETIRED: tuple[tuple[LiteralString, str], ...] = (
    ("MATERIALIZED VIEW", "mv_places_brand"),
    ("MATERIALIZED VIEW", "mv_places_spider"),
    ("MATERIALIZED VIEW", "mv_matches"),
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
        # The matches join mv_places and atp_places, so they have to be
        # rebuilt when either moves — and when MATCHED_POI_SQL itself changes,
        # which the revision covers.
        # normalize_phone is an input too: the view's SQL calls it without
        # carrying its body, so changing the phone key would move the matches
        # while leaving the signature untouched.
        signature = _matview.signature(
            app_version(),
            last_import_date(conn, "osm"),
            last_import_date(conn, "atp"),
            last_import_comment(conn, "nsi"),  # the NSI version string
        )
        views = ("mv_matches", "mv_places_brand", "mv_places_spider")
        if all(_matview.is_current(conn, view, signature) for view in views):
            logger.info("mv_places_brand already up-to-date, skipping")
            return

        # Built beside the live views, then swapped in: the site keeps reading
        # the old rows for the minutes the join takes, and only waits on the
        # exclusive lock for the swap at the very end. A DROP first would hold
        # that lock for the whole build — every request hanging on it. One
        # transaction: a failure leaves the old views exactly as they were.
        # The counters aggregate mv_matches_new, so the three agree.
        with conn.cursor() as cur:
            for view in views:
                cur.execute(
                    sql.SQL("DROP MATERIALIZED VIEW IF EXISTS {}").format(
                        sql.Identifier(f"{view}_new")
                    )
                )
            logger.info("Creating mv_matches...")
            cur.execute(mv_matches_sql("mv_matches_new"))
            _matview.create_indexes(cur, "mv_matches_new", MATCHES_INDEXES)
            logger.info("Creating mv_places_brand and mv_places_spider...")
            cur.execute(mv_places_brand_sql("mv_places_brand_new", "mv_matches_new"))
            cur.execute(mv_places_spider_sql("mv_places_spider_new", "mv_matches_new"))
            for view in views:
                _matview.stamp(cur, f"{view}_new", signature)
            _matview.swap(
                cur, "MATERIALIZED VIEW", "mv_matches", "mv_matches_new", tuple(MATCHES_INDEXES)
            )
            _matview.swap(cur, "MATERIALIZED VIEW", "mv_places_brand", "mv_places_brand_new")
            _matview.swap(cur, "MATERIALIZED VIEW", "mv_places_spider", "mv_places_spider_new")
            _drop_retired(cur)
        conn.commit()
        logger.info("mv_places_brand created")
    finally:
        conn.close()
