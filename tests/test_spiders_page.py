from psycopg.rows import dict_row

from src.routes.spiders import SPIDERS_SQL, cancellation_reasons
from tests.conftest import Connection


def test_history_is_borrowed_from_the_brands_and_deposit_counted_per_spider(
    migrated_conn: Connection,
) -> None:
    conn = migrated_conn
    conn.execute("""
        CREATE TEMP TABLE atp_places (id TEXT, spider_id TEXT, brand_wikidata TEXT, brand TEXT);
        INSERT INTO atp_places VALUES
            ('1', 'shop_fr', 'Q1', 'Shop'), ('2', 'shop_fr', 'Q1', 'Shop'),
            ('3', 'aggregator', 'Q1', 'Shop'), ('4', 'aggregator', 'Q2', 'Other');
        CREATE TEMP TABLE atp_spiders (spider TEXT, filename TEXT, errors INT8, features INT8,
                                  elapsed_time FLOAT8, updated_at TIMESTAMPTZ, log_url TEXT);
        INSERT INTO atp_spiders VALUES
            ('shop_fr', 'locations/spiders/shop_fr.py', 0, 900, 1, NOW(), NULL),
            ('aggregator', 'locations/spiders/aggregator.py', 2, 50, 1, NULL, NULL);
        CREATE TEMP TABLE mv_places_spider (spider_id TEXT, matched INT8);
        INSERT INTO mv_places_spider VALUES ('shop_fr', 1);
        INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, import_date, status, items_count, wave, comment)
        VALUES ('Q1', 'Shop', 1, NOW() - INTERVAL '2 days', 'success', 10, 1, NULL),
               ('Q1', 'Shop', 1, NOW() - INTERVAL '1 day', 'cancelled', 0, 1, 'closed'),
               ('Q2', 'Other', 1, NOW() - INTERVAL '3 years', 'success', 5, 1, NULL);
    """)
    try:
        rows = {
            r["spider"]: r
            for r in conn.cursor(row_factory=dict_row).execute(SPIDERS_SQL).fetchall()
        }
    finally:
        conn.rollback()

    shop, agg = rows["shop_fr"], rows["aggregator"]
    assert (shop["scraped"], shop["matched"]) == (2, 1)
    assert shop["match_rate"] == 50
    assert shop["last_status"] == "cancelled"
    # The last integration, whichever brand it was: the one to open.
    assert shop["last_comment"] == "closed"
    assert shop["last_id"] == agg["last_id"]
    assert (agg["scraped"], agg["matched"]) == (2, 0)
    assert agg["brands"] == "Other / Shop"
    assert agg["brand_list"] == [["Q2", "Other"], ["Q1", "Shop"]]
    assert agg["last_status"] == "cancelled"


def test_cancellation_reasons_gather_every_poi_turned_down() -> None:
    comment = (
        '[{"reasons": ["phone_wrong"], "comment": "old number"},'
        ' {"reasons": ["phone_wrong", "website_generic"], "comment": ""}]'
    )
    assert cancellation_reasons(comment) == (["phone_wrong", "website_generic"], ["old number"])
    # Older than the quick-pick reasons: the text is all there is.
    assert cancellation_reasons("closed for good") == ([], ["closed for good"])
    assert cancellation_reasons(None) == ([], [])
