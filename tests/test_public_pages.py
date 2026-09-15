"""Every public page renders on a seeded database.

A smoke test, deliberately shallow: a page that raises on its SQL or its
template is the failure this catches, before a contributor does. What each
page computes has tests of its own where it matters.
"""

import pytest
from flask import template_rendered

import src.routes.history as history
import src.routes.misc as misc
import src.routes.stats as stats


@pytest.fixture
def seeded(migrated_conn, monkeypatch):
    for module in (history, stats):
        monkeypatch.setattr(module, "fetch_osm_users", lambda ids: {})
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand, mv_places_spider")
        cur.execute("""
            CREATE TABLE mv_places_brand (brand TEXT, brand_wikidata TEXT, subdivision_code TEXT,
                                          wave SMALLINT, total BIGINT);
            CREATE TABLE mv_places_spider (spider_id TEXT, matched BIGINT);
            INSERT INTO mv_places_brand VALUES ('Babylone', 'Q1', '75', 1, 4);
            INSERT INTO mv_places_spider VALUES ('babylone_fr', 4);
            INSERT INTO atp_places (id, spider_id, brand_wikidata, brand) VALUES ('a', 'babylone_fr', 'Q1', 'Babylone');
            INSERT INTO atp_spiders VALUES ('babylone_fr', 'locations/spiders/babylone_fr.py', 0, 4, 1.5, NOW()),
                                           ('broken_fr', 'locations/spiders/broken_fr.py', 3, 0, 0.1, NULL);
            INSERT INTO data_imports (type, date, status, comment) VALUES
                ('osm', NOW(), 'success', NULL), ('atp', NOW(), 'pending', NULL), ('nsi', NOW(), 'success', 'v1');
            INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id) VALUES ('Q9', 'Missing', 43);
        """)
        cur.execute("""
            INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, import_date, status, comment, items_count, tags_count, wave)
            VALUES ('Q2', 'Old', 42, NOW() - INTERVAL '2 months', 'success', NULL, 12, '{"phone": 12}', 1),
                   ('Q3', 'Broken', 43, NOW() - INTERVAL '1 month', 'error', 'OSM unreachable', 0, NULL, 1),
                   ('Q4', 'Reported', 44, NOW() - INTERVAL '1 week', 'cancelled',
                    '[{"osm_id": 1, "osm_type": "node", "reasons": ["wrong_brand"], "comment": "no"}]', 0, NULL, 2)
            RETURNING id
        """)
        ids = [r[0] for r in cur.fetchall()]
        cur.execute("""
            INSERT INTO import_subdivisions (import_id, subdivision_code, subdivision_name, items_count, osm_changeset_id, status, comment)
            VALUES (%s, '75', 'Paris', 12, 1000001, 'success', NULL),
                   (%s, '33', 'Gironde', 0, NULL, 'error_osm_api', 'timeout')
        """, (ids[0], ids[1]))
    migrated_conn.commit()
    yield ids
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand, mv_places_spider")
    migrated_conn.commit()


@pytest.fixture
def rendered(web_app):
    names = []
    record = lambda sender, template, context, **extra: names.append(template.name)
    template_rendered.connect(record, web_app)
    yield names
    template_rendered.disconnect(record, web_app)


PAGES = ["/", "/brands", "/spiders", "/history", "/stats", "/todo", "/docs", "/about"]


@pytest.mark.parametrize("path", PAGES)
def test_a_public_page_renders(web_app, seeded, rendered, path):
    res = web_app.test_client().get(path)
    assert res.status_code == 200, res.text[:300]
    assert rendered and not rendered[0].startswith("errors/")


@pytest.mark.parametrize("path", PAGES)
def test_a_public_page_renders_for_a_contributor_too(contributor, seeded, path):
    assert contributor.get(path).status_code == 200


@pytest.mark.parametrize(
    "query",
    ["?q=old", "?status=error", "?user=43", "?from=2026-01-01&to=2026-12-31", "?sort=brand&dir=asc", "?page=2", "?page=0"],
)
def test_the_history_takes_every_filter(web_app, seeded, query):
    assert web_app.test_client().get("/history" + query).status_code == 200


def test_the_history_detail_shows_each_integration(web_app, seeded):
    client = web_app.test_client()
    for entry_id in seeded:
        assert client.get(f"/history/{entry_id}").status_code == 200
    assert client.get("/history/999999").status_code == 404


@pytest.mark.parametrize("query", ["", "?period=30d", "?period=all", "?period=nonsense", "?user=42"])
def test_the_stats_take_every_period(web_app, seeded, query):
    assert web_app.test_client().get("/stats" + query).status_code == 200


@pytest.mark.parametrize("query", ["", "?run=failed", "?run=ok", "?q=baby", "?sort=scraped&dir=asc"])
def test_the_spiders_take_every_filter(web_app, seeded, query):
    assert web_app.test_client().get("/spiders" + query).status_code == 200


@pytest.mark.parametrize("path, mimetype", [
    ("/robots.txt", "text/plain"),
    ("/sitemap.xml", "application/xml"),
    ("/llms.txt", "text/plain"),
    ("/favicon.ico", "image/svg+xml"),
])
def test_the_language_free_resources_answer(web_app, seeded, path, mimetype):
    res = web_app.test_client().get(path)
    assert res.status_code == 200
    assert res.mimetype == mimetype


def test_an_unknown_page_is_not_found(web_app, seeded):
    assert web_app.test_client().get("/no-such-page").status_code == 404


def test_the_stats_api_answers_json_to_any_origin(web_app, seeded):
    response = web_app.test_client().get("/api/stats.json?from=2020-01-01")
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    body = response.get_json()
    assert body["kpi"]["pois"] == 12
    assert body["unit"] == "month"
