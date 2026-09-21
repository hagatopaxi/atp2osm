import functools
import logging
from collections.abc import Callable
from typing import ParamSpec
from urllib.parse import urlparse

import requests
from flask import Blueprint, Response, abort, redirect, request, session, url_for
from flask.typing import ResponseReturnValue
from oauthlib.oauth2 import OAuth2Error
from requests_oauthlib import OAuth2Session

from src.config import get_settings

logger = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

_settings = get_settings()
api_url = _settings.api_url
client_id = _settings.oauth_client_id
client_secret = _settings.oauth_client_secret
authorization_base_url = f"{api_url}/oauth2/authorize"
token_url = f"{api_url}/oauth2/token"
scope = ["write_api", "read_prefs"]


P = ParamSpec("P")


def auth_required(f: Callable[P, ResponseReturnValue]) -> Callable[P, ResponseReturnValue]:
    @functools.wraps(f)
    def decorator(*args: P.args, **kwargs: P.kwargs) -> ResponseReturnValue:
        if "user" not in session:
            abort(403)
        if "token" not in session:
            session.clear()
            return redirect("/?session_expired=1")
        return f(*args, **kwargs)

    return decorator


def get_oauth_redirect_uri() -> str:
    if _settings.app_base_url:
        return f"{_settings.app_base_url}/oauth-callback"
    return url_for("auth.oauth_callback", _external=True)


@auth_bp.route("/login", methods=["POST"])
def login() -> ResponseReturnValue:
    redirect_uri = get_oauth_redirect_uri()

    osm = OAuth2Session(client_id, redirect_uri=redirect_uri, scope=scope)
    # The oauthlib stubs leave `state` incomplete: it is the str oauthlib draws.
    authorization_url, state = osm.authorization_url(authorization_base_url)  # pyright: ignore[reportUnknownMemberType]
    session["oauth_state"] = str(state)

    data = request.get_json(silent=True)
    body: dict[str, object] = data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    next_url = str(body.get("next", "/"))
    # Protected against open-redirect attack, see https://owasp.org/www-community/attacks/open_redirect
    parsed = urlparse(next_url)
    if parsed.netloc or parsed.scheme or not next_url.startswith("/"):
        next_url = "/"
    session["oauth_next"] = next_url

    return authorization_url


@auth_bp.route("/oauth-callback")
def oauth_callback() -> ResponseReturnValue:
    if "error" in request.args:
        return "Authentication failed: " + request.args["error"], 401

    # The state ties the callback to the login that started it — and both
    # missing is no match either.
    state = request.args.get("state")
    if not state or state != session.get("oauth_state"):
        return "Invalid state parameter", 401

    redirect_uri = get_oauth_redirect_uri()

    osm = OAuth2Session(client_id, redirect_uri=redirect_uri, state=session["oauth_state"])
    # On every call, not just the token one: the OSM API drops connections
    # that come in as python-requests.
    osm.headers["User-Agent"] = f"atp2osm/{_settings.app_version}"

    authorization_response = request.url
    if redirect_uri.startswith("https://") and authorization_response.startswith("http://"):
        authorization_response = "https://" + authorization_response[7:]
    try:
        token = osm.fetch_token(  # pyright: ignore[reportUnknownMemberType] — incomplete stubs
            token_url,
            client_secret=client_secret,
            authorization_response=authorization_response,
        )
    except OAuth2Error:
        # A code replayed or expired — the back button, a refresh on this
        # URL: OSM refused the grant, the contributor signs in again.
        logger.info("OAuth grant refused", exc_info=True)
        return "Authentication failed", 401
    except requests.RequestException:
        logger.exception("OSM API unreachable during login")
        abort(502)
    try:
        response = osm.get(f"{api_url}/api/0.6/user/details.json")
        response.raise_for_status()
        details = response.json()["user"]
        user = {"osm_id": int(details["id"]), "name": str(details["display_name"])}
    except (requests.RequestException, ValueError, KeyError, TypeError):
        # Unreachable, or an answer that is not a user: OSM's side either way.
        logger.exception("OSM API did not answer the user details")
        abort(502)
    del session["oauth_state"]
    session["user"] = user
    session["token"] = dict(token)

    next_url = session.pop("oauth_next", "/")
    return redirect(next_url)


@auth_bp.route("/logout", methods=["POST"])
@auth_required
def logout() -> ResponseReturnValue:
    # clean the session
    session.clear()

    return Response(status=204)
