from psycopg.rows import dict_row

from src.routes.spiders import SPIDERS_SQL
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
                                  elapsed_time FLOAT8, updated_at TIMESTAMPTZ);
        INSERT INTO atp_spiders VALUES
            ('shop_fr', 'locations/spiders/shop_fr.py', 0, 900, 1, NOW()),
            ('aggregator', 'locations/spiders/aggregator.py', 2, 50, 1, NULL);
        CREATE TEMP TABLE mv_places_spider (spider_id TEXT, matched INT8);
        INSERT INTO mv_places_spider VALUES ('shop_fr', 1);
        CREATE TEMP TABLE mv_places_brand (brand TEXT, brand_wikidata TEXT, subdivision_code TEXT, wave INT, total INT8);
        INSERT INTO mv_places_brand VALUES ('Shop', 'Q1', '75', 1, 1), ('Shop', 'Q1', '75', 2, 1),
                                           ('Other', 'Q2', '33', 1, 4);
        INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, import_date, status, items_count, wave)
        VALUES ('Q1', 'Shop', 1, NOW() - INTERVAL '2 days', 'success', 10, 1),
               ('Q1', 'Shop', 1, NOW() - INTERVAL '1 day', 'cancelled', 0, 1),
               ('Q2', 'Other', 1, NOW() - INTERVAL '3 years', 'success', 5, 1);
    """)
    try:
        rows = {
            r["spider"]: r
            for r in conn.cursor(row_factory=dict_row).execute(SPIDERS_SQL).fetchall()
        }
    finally:
        conn.rollback()

    shop, agg = rows["shop_fr"], rows["aggregator"]
    assert (shop["scraped"], shop["matched"], shop["integrated"]) == (2, 1, 10)
    # Every wave: the recent success puts Q1's wave 1 under cooldown, its wave 2
    # stays; Q2's old success blocks nothing any more.
    assert (shop["to_integrate"], agg["to_integrate"]) == (1, 5)
    assert shop["last_status"] == "cancelled"
    # Q1 counts on both spiders — a plain sum, as specified.
    assert (agg["scraped"], agg["matched"], agg["integrated"]) == (2, 0, 15)
    assert agg["brands"] == "Other / Shop"
    assert agg["brand_list"] == [["Q2", "Other"], ["Q1", "Shop"]]
    assert agg["last_status"] == "cancelled"
