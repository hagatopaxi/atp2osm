"""Signing in with OSM: what the session ends up holding, and what it refuses.

The OAuth exchange is staged on the OAuth2Session the route builds: the
authorization URL, the token and the user details are the only three things
OSM says, and none of them crosses the network here.
"""

import pytest
import requests

import src.routes.auth as auth


class _Session:
    """An OAuth2Session whose OSM answers are staged."""

    token = {"access_token": "tok", "token_type": "Bearer"}
    user = {"user": {"id": 42, "display_name": "reviewer"}}
    down = False

    def __init__(self, client_id=None, redirect_uri=None, scope=None, state=None, token=None):
        self.headers = {}
        self.state = state

    def authorization_url(self, url):
        return f"{url}?client_id=x&state=abc", "abc"

    def fetch_token(self, url, client_secret=None, authorization_response=None):
        if self.down:
            raise requests.ConnectionError("osm down")
        return dict(self.token)

    def get(self, url):
        class R:
            def raise_for_status(self_):
                pass

            def json(self_):
                return _Session.user

        return R()


@pytest.fixture
def osm(monkeypatch):
    monkeypatch.setattr(auth, "OAuth2Session", _Session)
    monkeypatch.setattr(_Session, "down", False)
    return _Session


def login(client, next_url):
    return client.post("/login", json={"next": next_url})


# --- /login -------------------------------------------------------------------------


def test_login_answers_the_authorization_url_and_remembers_where_to_go(web_app, osm):
    with web_app.test_client() as client:
        res = login(client, "/brands/Q1/validate")
        assert res.status_code == 200
        assert res.text.startswith(f"{auth.authorization_base_url}?")
        with client.session_transaction() as sess:
            assert sess["oauth_state"] == "abc"
            assert sess["oauth_next"] == "/brands/Q1/validate"


@pytest.mark.parametrize(
    "next_url",
    ["https://evil.example/", "//evil.example/", "javascript:alert(1)", "brands", ""],
)
def test_login_never_sends_the_contributor_off_the_site(web_app, osm, next_url):
    """An open redirect would let a forged link land a signed-in session anywhere."""
    with web_app.test_client() as client:
        login(client, next_url)
        with client.session_transaction() as sess:
            assert sess["oauth_next"] == "/"


# --- /oauth-callback --------------------------------------------------------------------


def test_the_callback_signs_the_contributor_in_and_sends_them_on(web_app, osm):
    with web_app.test_client() as client:
        login(client, "/brands")
        res = client.get("/oauth-callback?code=c&state=abc")
        assert res.status_code == 302
        assert res.headers["Location"].endswith("/brands")
        with client.session_transaction() as sess:
            assert sess["user"] == {"osm_id": 42, "name": "reviewer"}
            assert sess["token"]["access_token"] == "tok"
            assert "oauth_state" not in sess
            assert "oauth_next" not in sess


def test_a_callback_with_a_foreign_state_is_refused(web_app, osm):
    """The state ties the callback to the login that started it (CSRF)."""
    with web_app.test_client() as client:
        login(client, "/")
        res = client.get("/oauth-callback?code=c&state=forged")
        assert res.status_code == 401
        with client.session_transaction() as sess:
            assert "user" not in sess


def test_a_callback_without_a_login_is_refused(web_app, osm):
    assert web_app.test_client().get("/oauth-callback?code=c&state=abc").status_code == 401


def test_a_denied_authorization_is_reported(web_app, osm):
    with web_app.test_client() as client:
        login(client, "/")
        res = client.get("/oauth-callback?error=access_denied&state=abc")
        assert res.status_code == 401
        with client.session_transaction() as sess:
            assert "user" not in sess


def test_osm_down_during_the_exchange_is_a_bad_gateway(web_app, osm, monkeypatch):
    monkeypatch.setattr(_Session, "down", True)
    with web_app.test_client() as client:
        login(client, "/")
        assert client.get("/oauth-callback?code=c&state=abc").status_code == 502
        with client.session_transaction() as sess:
            assert "user" not in sess


# --- /logout ------------------------------------------------------------------------------


def test_logout_forgets_everything(contributor):
    assert contributor.post("/logout").status_code == 204
    with contributor.session_transaction() as sess:
        assert dict(sess) == {}


def test_logout_needs_a_session(web_app):
    assert web_app.test_client().post("/logout").status_code == 403
