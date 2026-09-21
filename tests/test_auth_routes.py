"""Signing in with OSM: what the session ends up holding, and what it refuses.

The OAuth exchange is staged on the OAuth2Session the route builds: the
authorization URL, the token and the user details are the only three things
OSM says, and none of them crosses the network here.
"""

from typing import Any, ClassVar

import pytest
import requests
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from src.routes import auth


class _Details:
    """The user details answer, as requests hands it over."""

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return _Session.user


class _Session:
    """An OAuth2Session whose OSM answers are staged."""

    token: ClassVar[dict[str, str]] = {"access_token": "tok", "token_type": "Bearer"}
    user: ClassVar[dict[str, Any]] = {"user": {"id": 42, "display_name": "reviewer"}}
    down = False

    def __init__(
        self,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        scope: list[str] | None = None,
        state: str | None = None,
        token: dict[str, str] | None = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.state = state

    def authorization_url(self, url: str) -> tuple[str, str]:
        return f"{url}?client_id=x&state=abc", "abc"

    def fetch_token(
        self,
        url: str,
        client_secret: str | None = None,
        authorization_response: str | None = None,
    ) -> dict[str, str]:
        if self.down:
            raise requests.ConnectionError("osm down")
        return dict(self.token)

    def get(self, url: str) -> _Details:
        return _Details()


@pytest.fixture
def osm(monkeypatch: pytest.MonkeyPatch) -> type[_Session]:
    monkeypatch.setattr(auth, "OAuth2Session", _Session)
    monkeypatch.setattr(_Session, "down", False)
    return _Session


def login(client: FlaskClient, next_url: str) -> TestResponse:
    return client.post("/login", json={"next": next_url})


# --- /login -------------------------------------------------------------------------


def test_login_answers_the_authorization_url_and_remembers_where_to_go(
    web_app: Flask, osm: type[_Session]
) -> None:
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
def test_login_never_sends_the_contributor_off_the_site(
    web_app: Flask, osm: type[_Session], next_url: str
) -> None:
    """An open redirect would let a forged link land a signed-in session anywhere."""
    with web_app.test_client() as client:
        login(client, next_url)
        with client.session_transaction() as sess:
            assert sess["oauth_next"] == "/"


# --- /oauth-callback --------------------------------------------------------------------


def test_the_callback_signs_the_contributor_in_and_sends_them_on(
    web_app: Flask, osm: type[_Session]
) -> None:
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


def test_a_callback_with_a_foreign_state_is_refused(web_app: Flask, osm: type[_Session]) -> None:
    """The state ties the callback to the login that started it (CSRF)."""
    with web_app.test_client() as client:
        login(client, "/")
        res = client.get("/oauth-callback?code=c&state=forged")
        assert res.status_code == 401
        with client.session_transaction() as sess:
            assert "user" not in sess


def test_a_callback_without_a_login_is_refused(web_app: Flask, osm: type[_Session]) -> None:
    assert web_app.test_client().get("/oauth-callback?code=c&state=abc").status_code == 401


def test_a_denied_authorization_is_reported(web_app: Flask, osm: type[_Session]) -> None:
    with web_app.test_client() as client:
        login(client, "/")
        res = client.get("/oauth-callback?error=access_denied&state=abc")
        assert res.status_code == 401
        with client.session_transaction() as sess:
            assert "user" not in sess


def test_osm_down_during_the_exchange_is_a_bad_gateway(
    web_app: Flask, osm: type[_Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_Session, "down", True)
    with web_app.test_client() as client:
        login(client, "/")
        assert client.get("/oauth-callback?code=c&state=abc").status_code == 502
        with client.session_transaction() as sess:
            assert "user" not in sess


# --- /logout ------------------------------------------------------------------------------


def test_logout_forgets_everything(contributor: FlaskClient) -> None:
    assert contributor.post("/logout").status_code == 204
    with contributor.session_transaction() as sess:
        assert dict(sess) == {}


def test_logout_needs_a_session(web_app: Flask) -> None:
    assert web_app.test_client().post("/logout").status_code == 403
