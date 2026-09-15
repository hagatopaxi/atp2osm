"""Interface language: URL prefix, selection, and locale names.

One instance serves one country, but a country may have several languages.
`App.locales` is an ordered tuple whose first element is the default one.

The language lives in the path — `/de/brands` — because a subdirectory is what
search engines segment cleanly, unlike a query parameter. A WSGI middleware
moves the prefix into `SCRIPT_NAME`, so every `url_for` carries the current
language without a single route or template having to know about it.
"""

import logging
import re

from babel import Locale, UnknownLocaleError
from flask import request
from flask_babel import Babel, format_date, format_datetime, get_locale
from werkzeug.utils import redirect
from werkzeug.wrappers import Request

from src.config import TRANSLATIONS_DIR

logger = logging.getLogger(__name__)

COOKIE_NAME = "lang"
COOKIE_MAX_AGE = 365 * 24 * 3600
ENVIRON_KEY = "atp2osm.locale"

# ponytail: a two-letter first segment is assumed to be a language code, so a
# page route may never start with one. The translated paths below are the guard.
LANG_CODE = re.compile(r"[a-z]{2}(-[a-z]{2})?", re.IGNORECASE)


class LanguagePrefix:
    """Serve `/de/brands` as `/brands` in German, and send `/brands` to a language.

    Only the translated paths are redirected; assets, the API and the OAuth
    callback stay language-free, and no request that carries a body is moved.
    """

    def __init__(self, wsgi_app, locales, translated):
        self.wsgi_app = wsgi_app
        self.locales = locales
        self.translated = translated

    def _is_translated(self, path):
        return any(
            path == p or (p != "/" and path.startswith(p + "/"))
            for p in self.translated
        )

    def negotiate(self, environ):
        """Language of a request that does not name one: cookie, then browser."""
        req = Request(environ)
        cookie = req.cookies.get(COOKIE_NAME)
        if cookie in self.locales:
            return cookie
        return req.accept_languages.best_match(self.locales) or self.locales[0]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        head, _, tail = path[1:].partition("/")

        if head in self.locales:
            # A language-free resource — sitemap.xml, llms.txt, an asset — is
            # served under its own URL only: a prefixed one would be a second
            # address for the same bytes.
            if not self._is_translated("/" + tail):
                return self._redirect(environ, start_response, "/" + tail)
            environ["SCRIPT_NAME"] = environ.get("SCRIPT_NAME", "") + "/" + head
            environ["PATH_INFO"] = "/" + tail
            environ[ENVIRON_KEY] = head
            return self.wsgi_app(environ, start_response)

        # An unknown language code is dropped; a missing one is added.
        if LANG_CODE.fullmatch(head) and self._is_translated("/" + tail):
            path = "/" + tail
        elif not self._is_translated(path):
            return self.wsgi_app(environ, start_response)

        return self._redirect(
            environ, start_response, f"/{self.negotiate(environ)}{path}"
        )

    def _redirect(self, environ, start_response, path):
        """Send a GET elsewhere, query string kept; leave any body alone."""
        if environ.get("REQUEST_METHOD") not in ("GET", "HEAD"):
            return self.wsgi_app(environ, start_response)
        query = environ.get("QUERY_STRING", "")
        target = path + (f"?{query}" if query else "")
        return redirect(target)(environ, start_response)


def locale_name(code):
    """Language name written in that language — "Deutsch", not "Allemand"."""
    try:
        return Locale.parse(str(code)).get_display_name(str(code)).capitalize()
    except (UnknownLocaleError, ValueError):
        return str(code)


def lang_url(code):
    """Current page in `code` — path, filters, sort and pagination kept."""
    query = request.query_string.decode()
    return f"/{code}{request.path}" + (f"?{query}" if query else "")


def localized_url(code):
    """Absolute canonical URL of the current page in `code`, without its query."""
    return f"{request.host_url.rstrip('/')}/{code}{request.path}"


def default_url():
    """Absolute language-free URL — the one that negotiates, for `x-default`."""
    return f"{request.host_url.rstrip('/')}{request.path}"


def localized_url_for(endpoint, code=None, **values):
    """Absolute URL of a page in a given language, from a language-free route."""
    from flask import url_for

    code = code or get_locale()
    return f"{request.host_url.rstrip('/')}/{code}{url_for(endpoint, **values)}"


def language_free(url):
    """Strip the language prefix from a URL that carries no language.

    `url_for` prefixes everything, because `SCRIPT_NAME` says so. The handful of
    language-free routes linked from a page — the export API — go through this.
    """
    root = request.script_root
    return url[len(root):] if root and url.startswith(root + "/") else url


def static_url(filename, external=False):
    """Assets are language-free, so they must escape the language prefix."""
    base = request.host_url.rstrip("/") if external else ""
    return f"{base}/static/{filename}"


DATE_FORMATS = {"short": "dd/MM/yyyy", "medium": "d MMM yyyy"}
DATETIME_FORMATS = {"short": "dd/MM/yyyy HH:mm", "medium": "d MMM yyyy HH:mm"}


def init_app(app, locales, translated, timezone="UTC", extra_translations=""):
    """Wire the prefix middleware, Babel, the cookie and the Jinja globals."""
    app.wsgi_app = LanguagePrefix(app.wsgi_app, locales, translated)
    app.config["BABEL_DEFAULT_LOCALE"] = locales[0]
    # Flask-Babel merges the directories in order, so the deployment's own
    # catalogs come last and win over the ones the image ships.
    app.config["BABEL_TRANSLATION_DIRECTORIES"] = ";".join(
        [str(TRANSLATIONS_DIR)] + ([extra_translations] if extra_translations else [])
    )
    app.config["BABEL_DEFAULT_TIMEZONE"] = timezone
    # One Babel per app, not one for the process: it carries the cache of
    # merged catalogs, and two apps rarely read the same directories.
    Babel().init_app(
        app, locale_selector=lambda: request.environ.get(ENVIRON_KEY) or locales[0]
    )

    @app.after_request
    def remember_language(response):
        """Remember the language browsed, so `/` lands on it next time."""
        chosen = request.environ.get(ENVIRON_KEY)
        if chosen and request.cookies.get(COOKIE_NAME) != chosen:
            response.set_cookie(
                COOKIE_NAME, chosen, max_age=COOKIE_MAX_AGE, samesite="Lax"
            )
        return response

    # Day-month-year whatever the language: CLDR's `en` is en_US, and its
    # `M/d/yy` reads as a US date to every reader of the country served. The
    # locale still provides the month names and the timezone.
    app.jinja_env.filters["dateformat"] = lambda d, f="short": format_date(
        d, DATE_FORMATS.get(f, f)
    )
    app.jinja_env.filters["datetimeformat"] = lambda d, f="short": format_datetime(
        d, DATETIME_FORMATS.get(f, f)
    )

    app.jinja_env.globals.update(
        get_locale=get_locale,
        locale_name=locale_name,
        lang_url=lang_url,
        localized_url=localized_url,
        default_url=default_url,
        static_url=static_url,
        localized_url_for=localized_url_for,
        locales=locales,
    )

    app.jinja_env.filters["language_free"] = language_free

    @app.context_processor
    def inject_canonical():
        return {"canonical_url": request.base_url}
