"""No request answers 500.

Whatever a client sends — a bad date, a body that is not an object, a
callback replayed, a coordinate that is not a number — is answered with the
status that names it. The only 5xx left are those that say what is down
(502 for a source, 503 for OSM), and they are chosen, never raised.

Every case here was a stack trace before it was a test.
"""

import pytest
from oauthlib.oauth2 import InvalidGrantError
from psycopg.rows import dict_row

import src.routes.auth as auth
import src.routes.brands as brands
import src.routes.misc as misc
from src.matching import WAVES_BY_NUMBER
from src.upload import BulkUpload


@pytest.fixture(autouse=True)
def _no_user_lookup(monkeypatch):
    import src.routes.history
    import src.routes.stats
    import src.routes.todo

    for module in (src.routes.history, src.routes.stats, src.routes.todo, brands):
        monkeypatch.setattr(module, "fetch_osm_users", lambda ids: {})


# --- Query strings ----------------------------------------------------------------


@pytest.mark.parametrize("path", ["/history", "/stats", "/todo", "/api/export/history.json"])
@pytest.mark.parametrize("query", ["from=hello", "to=2026-13-45", "from=2026-01-01&to=", "from=1"])
def test_a_date_that_is_not_one_is_ignored(web_app, migrated_conn, path, query):
    res = web_app.test_client().get(f"{path}?{query}")
    assert res.status_code == 200


@pytest.fixture
def views(migrated_conn):
    """The two views the pipeline builds, empty."""
    with migrated_conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS mv_places_brand, mv_places_spider;
            CREATE TABLE mv_places_brand (brand TEXT, brand_wikidata TEXT, subdivision_code TEXT,
                                          wave SMALLINT, total BIGINT);
            CREATE TABLE mv_places_spider (spider_id TEXT, matched BIGINT);
        """)
    migrated_conn.commit()
    yield
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand, mv_places_spider")
    migrated_conn.commit()


@pytest.mark.parametrize("path", ["/history", "/stats", "/todo", "/brands", "/spiders"])
def test_junk_in_every_filter_is_ignored(web_app, views, path):
    query = "user=abc&page=abc&sort=';drop&dir=up&status=<x>&wave=x&period=%00&q=%ff&run=x"
    assert web_app.test_client().get(f"{path}?{query}").status_code == 200


@pytest.mark.parametrize("path", ["/brands", "/spiders", "/brands/Q1/validate"])
def test_before_the_first_refresh_the_data_pages_say_so(contributor, migrated_conn, path):
    """A new instance: the views do not exist yet. Not our crash."""
    res = contributor.get(path)
    assert res.status_code == 503
    assert "premier rafraîchissement" in res.text  # the test locale is French


# --- The static map ---------------------------------------------------------------


@pytest.mark.parametrize("path", ["/staticmap/abc/def", "/staticmap/999/999", "/staticmap/2.35/91", "/staticmap/nan/48"])
def test_a_coordinate_that_is_not_one_is_not_found(web_app, migrated_conn, path):
    assert web_app.test_client().get(path).status_code == 404


def test_a_tile_server_down_is_a_bad_gateway(web_app, migrated_conn, monkeypatch):
    def down(self, zoom=None):
        raise RuntimeError("could not download 4 tiles")

    monkeypatch.setattr(misc.StaticMap, "render", down)
    res = web_app.test_client().get("/staticmap/2.35/48.85")
    assert res.status_code == 502
    # Not cached: the next request asks again.
    monkeypatch.setattr(misc.StaticMap, "render", lambda self, zoom=None: _image())
    assert web_app.test_client().get("/staticmap/2.35/48.85").status_code == 200


def _image():
    from PIL import Image
    return Image.new("RGB", (1, 1))


# --- Bodies that are not objects --------------------------------------------------


@pytest.mark.parametrize("body", ["[]", '"x"', "null", "12"])
def test_a_todo_body_that_is_not_an_object_is_a_bad_request(contributor, migrated_conn, body):
    res = contributor.post("/todo", data=body, content_type="application/json")
    assert res.status_code == 400
    res = contributor.put("/todo/1", data=body, content_type="application/json")
    assert res.status_code == 400


def test_a_todo_body_that_is_not_json_is_a_bad_request(contributor, migrated_conn):
    res = contributor.post("/todo", data="brand_name=x", content_type="application/x-www-form-urlencoded")
    assert res.status_code in (400, 415)
    res = contributor.post("/todo", data="{not json", content_type="application/json")
    assert res.status_code == 400


@pytest.mark.parametrize("body", ["[]", "null", "{not json"])
def test_a_rejection_body_that_is_not_an_object_is_a_bad_request(contributor, migrated_conn, monkeypatch, body):
    monkeypatch.setattr(brands, "get_batch", lambda w: ([], [], WAVES_BY_NUMBER[1]))
    res = contributor.post("/brands/Q1/report-error", data=body, content_type="application/json")
    assert res.status_code == 400
    assert migrated_conn.execute("SELECT count(*) FROM import_history").fetchone()[0] == 0


@pytest.mark.parametrize("body", ["[]", "null", "{not json", ""])
def test_a_login_body_that_is_not_an_object_still_signs_in(web_app, migrated_conn, monkeypatch, body):
    """The body only carries where to go next: garbage means the home page."""
    class S:
        def __init__(self, *a, **k): pass
        def authorization_url(self, url): return f"{url}?state=abc", "abc"

    monkeypatch.setattr(auth, "OAuth2Session", S)
    with web_app.test_client() as client:
        res = client.post("/login", data=body, content_type="application/json")
        assert res.status_code == 200
        with client.session_transaction() as sess:
            assert sess["oauth_next"] == "/"


# --- The OAuth callback -------------------------------------------------------------


def test_a_callback_without_any_state_is_refused(web_app, migrated_conn):
    """No login started, no state sent: None equals None is not a match."""
    assert web_app.test_client().get("/oauth-callback?code=c").status_code == 401


class _Exchange:
    """An OAuth2Session whose token exchange is staged."""
    outcome = None
    details = {"user": {"id": 42, "display_name": "reviewer"}}

    def __init__(self, *a, **k):
        self.headers = {}

    def authorization_url(self, url):
        return f"{url}?state=abc", "abc"

    def fetch_token(self, *a, **k):
        if isinstance(_Exchange.outcome, Exception):
            raise _Exchange.outcome
        return {"access_token": "tok"}

    def get(self, url):
        class R:
            def raise_for_status(self_): pass
            def json(self_): return _Exchange.details
        return R()


@pytest.fixture
def exchange(monkeypatch):
    monkeypatch.setattr(auth, "OAuth2Session", _Exchange)
    monkeypatch.setattr(_Exchange, "outcome", None)
    monkeypatch.setattr(_Exchange, "details", {"user": {"id": 42, "display_name": "reviewer"}})
    return _Exchange


def test_a_replayed_or_expired_code_is_refused_not_a_crash(web_app, migrated_conn, exchange, monkeypatch):
    """Back button, refresh on the callback URL: OSM refuses the grant."""
    monkeypatch.setattr(exchange, "outcome", InvalidGrantError())
    with web_app.test_client() as client:
        client.post("/login", json={"next": "/"})
        res = client.get("/oauth-callback?code=used&state=abc")
        assert res.status_code == 401
        with client.session_transaction() as sess:
            assert "user" not in sess


@pytest.mark.parametrize("details", [{}, {"user": {}}, [], None])
def test_user_details_without_a_user_are_a_bad_gateway(web_app, migrated_conn, exchange, monkeypatch, details):
    monkeypatch.setattr(exchange, "details", details)
    with web_app.test_client() as client:
        client.post("/login", json={"next": "/"})
        assert client.get("/oauth-callback?code=c&state=abc").status_code == 502


# --- The upload -----------------------------------------------------------------------


def test_an_upload_whose_log_cannot_be_written_is_still_recorded(contributor, migrated_conn, monkeypatch):
    """The changesets are on OSM: the row that says so must be written."""
    change = {
        "id": 1, "version": 1, "node_type": "node",
        "tag": {"brand": "Babylone", "brand:wikidata": "Q1", "email": "a@b.fr"},
        "members": [], "subdivision_code": "75", "subdivision_name": "Paris",
        "atp_brand": "Babylone",
    }
    monkeypatch.setattr(brands, "get_batch", lambda w: ([change], {}, WAVES_BY_NUMBER[1]))
    monkeypatch.setattr(BulkUpload, "_write_osc", lambda *a, **k: None)

    def disk_full(self):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(BulkUpload, "save_log_file", disk_full)

    res = contributor.post("/brands/Q1/upload")

    assert res.status_code == 200
    with migrated_conn.cursor(row_factory=dict_row) as cur:
        (row,) = cur.execute("SELECT status, items_count FROM import_history").fetchall()
    assert (row["status"], row["items_count"]) == ("success", 1)
