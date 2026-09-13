from psycopg.rows import dict_row

from src.matching import UNBLOCKED_WAVES_SQL


def _unblocked(conn, spider_updated_at):
    conn.execute("""
        CREATE TEMP TABLE mv_places_brand (brand TEXT, brand_wikidata TEXT, subdivision_code TEXT, wave INT, total INT8);
        INSERT INTO mv_places_brand VALUES ('Shop', 'Q1', '75', 1, 3), ('Shop', 'Q1', '75', 2, 1);
        INSERT INTO atp_places VALUES ('1', 'shop_fr', 'Q1', 'Shop');
        INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, import_date, status, items_count, wave)
        VALUES ('Q1', 'Shop', 1, NOW() - INTERVAL '1 year', 'cancelled', 0, 1);
    """)
    conn.execute(
        "INSERT INTO atp_spiders (spider, updated_at) VALUES ('shop_fr', %s)",
        (spider_updated_at,),
    )
    try:
        return conn.cursor(row_factory=dict_row).execute(UNBLOCKED_WAVES_SQL).fetchall()
    finally:
        conn.rollback()


def test_a_cancelled_brand_stays_hidden_until_one_of_its_spiders_changes(migrated_conn):
    # No cooldown: a year later, an unchanged spider still hides the brand —
    # every wave of it, not just the one turned down.
    assert _unblocked(migrated_conn, "2020-01-01") == []
    assert _unblocked(migrated_conn, None) == []


def test_a_spider_edited_after_the_cancellation_brings_the_brand_back(migrated_conn):
    rows = _unblocked(migrated_conn, "2030-01-01")
    assert [(r["brand_wikidata"], r["total"]) for r in rows] == [("Q1", 3), ("Q1", 1)]
