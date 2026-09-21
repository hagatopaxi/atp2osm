"""Language prefix in the path: routing, fallback redirect and switching URL."""

from pathlib import Path

import pytest
from flask import Flask
from flask_babel import get_locale
from werkzeug.test import TestResponse

from src import i18n

LOCALES = ("fr", "de", "it")
TRANSLATED = ("/", "/brands")


@pytest.fixture
def app() -> Flask:
    app = Flask(__name__)
    i18n.init_app(app, LOCALES, (*TRANSLATED, "/when"), "Europe/Paris")

    @app.route("/brands")
    def brands() -> str:
        return f"{get_locale()}|{i18n.lang_url('de')}"

    @app.route("/api/export")
    def export() -> str:
        return "csv"

    return app


def get(
    app: Flask,
    path: str,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    *,
    follow: bool = False,
) -> TestResponse:
    client = app.test_client()
    for name, value in (cookies or {}).items():
        client.set_cookie(name, value)
    return client.get(path, headers=headers or {}, follow_redirects=follow)


def test_the_prefix_selects_the_language(app: Flask) -> None:
    assert get(app, "/it/brands").text.startswith("it|")
    assert get(app, "/fr/brands").text.startswith("fr|")


def test_the_prefix_wins_over_the_cookie(app: Flask) -> None:
    assert get(app, "/it/brands", cookies={"lang": "de"}).text.startswith("it|")


def test_a_missing_language_redirects_to_the_negotiated_one(app: Flask) -> None:
    assert get(app, "/brands").headers["Location"].endswith("/fr/brands")
    assert get(app, "/brands", cookies={"lang": "de"}).headers["Location"].endswith("/de/brands")
    headers = {"Accept-Language": "it-CH,it;q=0.9"}
    assert get(app, "/brands", headers=headers).headers["Location"].endswith("/it/brands")


def test_an_unknown_language_falls_back_to_the_default(app: Flask) -> None:
    assert get(app, "/es/brands").headers["Location"].endswith("/fr/brands")


def test_the_redirect_keeps_the_query_string(app: Flask) -> None:
    location = get(app, "/brands?sort=name&page=2").headers["Location"]
    assert location.endswith("/fr/brands?sort=name&page=2")


def test_language_free_paths_are_left_alone(app: Flask) -> None:
    assert get(app, "/api/export").status_code == 200


def test_the_language_browsed_is_remembered(app: Flask) -> None:
    assert "lang=de" in get(app, "/de/brands").headers.get("Set-Cookie", "")
    assert not get(app, "/api/export").headers.get("Set-Cookie")


def test_lang_url_keeps_path_and_query(app: Flask) -> None:
    _, url = get(app, "/it/brands?sort=name&page=2").text.split("|")
    assert url == "/de/brands?sort=name&page=2"


def test_locale_name_is_written_in_its_own_language(app: Flask) -> None:
    assert i18n.locale_name("de") == "Deutsch"
    assert i18n.locale_name("it") == "Italiano"


def test_alternate_urls_are_absolute_and_language_free_by_default(app: Flask) -> None:
    with app.test_request_context("/brands", base_url="http://localhost/it"):
        assert i18n.localized_url("de") == "http://localhost/de/brands"
        assert i18n.default_url() == "http://localhost/brands"
        assert i18n.static_url("img/a.png") == "/static/img/a.png"
        assert i18n.static_url("img/a.png", external=True) == ("http://localhost/static/img/a.png")


def test_language_free_strips_the_prefix(app: Flask) -> None:
    with app.test_request_context("/x", base_url="http://localhost/it"):
        assert i18n.language_free("/it/api/export.csv") == "/api/export.csv"
        assert i18n.language_free("/italy") == "/italy"
    with app.test_request_context("/x"):
        assert i18n.language_free("/api/export.csv") == "/api/export.csv"


def test_dates_follow_the_language_and_the_country_timezone(app: Flask) -> None:
    import datetime

    from flask_babel import format_datetime

    moment = datetime.datetime(2026, 8, 31, 6, 48, tzinfo=datetime.UTC)

    @app.route("/when")
    def when() -> str:
        return format_datetime(moment, "short")

    app.config["BABEL_DEFAULT_TIMEZONE"] = "Europe/Paris"
    assert get(app, "/fr/when").text == "31/08/2026 08:48"
    assert get(app, "/de/when").text == "31.08.26, 08:48"


def test_every_message_is_translated() -> None:
    """A missing or fuzzy translation silently falls back to the English msgid."""
    from pathlib import Path

    from babel.messages.pofile import read_po

    from src.config import TRANSLATIONS_DIR

    for path in Path(TRANSLATIONS_DIR).glob("*/LC_MESSAGES/messages.po"):
        with path.open("rb") as f:
            catalog = read_po(f)
        for message in catalog:
            if not message.id:
                continue
            strings = message.string if isinstance(message.string, tuple) else (message.string,)
            assert all(strings), f"{path}: untranslated {message.id!r}"
            assert not message.fuzzy, f"{path}: fuzzy {message.id!r}"


def test_every_template_compiles() -> None:
    """A `{% trans %}` block that is malformed only shows up at render time."""
    from flask import Flask

    from src import i18n
    from src.config import TEMPLATE_DIR

    app = Flask(__name__, template_folder=TEMPLATE_DIR)
    i18n.init_app(app, ("fr",), ("/",))
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)


def test_language_free_paths_are_not_served_under_a_prefix() -> None:
    """One resource, one URL: /fr/sitemap.xml redirects to /sitemap.xml."""
    from flask import Flask

    from src import i18n

    app = Flask(__name__)
    i18n.init_app(app, ("fr", "en"), ("/", "/docs"))

    @app.route("/sitemap.xml")
    def sitemap() -> str:
        return "sitemap"

    client = app.test_client()
    assert client.get("/fr/sitemap.xml").headers["Location"] == "/sitemap.xml"
    assert client.get("/sitemap.xml").data == b"sitemap"


def test_every_key_the_scripts_ask_for_is_rendered() -> None:
    """A key missing from the JSON block shows its own slug to the user."""
    import json
    import re
    from pathlib import Path

    from flask import Flask, render_template

    from src import i18n
    from src.config import PROJECT_ROOT, TEMPLATE_DIR

    app = Flask(__name__, template_folder=TEMPLATE_DIR)
    i18n.init_app(app, ("fr",), ("/",))
    with app.test_request_context("/"):
        rendered = render_template("_js_strings.html")
    block = re.search(r">\s*(\{.*\})\s*<", rendered, re.DOTALL)
    assert block is not None
    keys = set(json.loads(block.group(1)))

    asked: set[str] = set()
    for script in (Path(PROJECT_ROOT) / "static" / "js").glob("*.js"):
        asked |= set(re.findall(r"""\bt\(\s*["'](\w+)["']""", script.read_text()))

    assert asked <= keys, f"not rendered: {sorted(asked - keys)}"


def test_a_mounted_catalog_wins_over_the_shipped_one(tmp_path: Path) -> None:
    """A deployment adds a language, or fixes a wording, without a fork."""
    import subprocess

    from flask import Flask
    from flask_babel import gettext

    from src import i18n

    catalog = tmp_path / "fr" / "LC_MESSAGES"
    catalog.mkdir(parents=True)
    (catalog / "messages.po").write_text(
        'msgid ""\nmsgstr "Content-Type: text/plain; charset=utf-8\\n"\n\n'
        'msgid "Statistics"\nmsgstr "Chiffres"\n'
    )
    # A fixed command line, no shell; pybabel comes with the dependencies.
    subprocess.run(["pybabel", "compile", "-d", str(tmp_path)], check=True)  # noqa: S603, S607

    app = Flask(__name__)
    i18n.init_app(app, ("fr",), ("/",), "Europe/Paris", str(tmp_path))
    with app.test_request_context("/", environ_overrides={i18n.ENVIRON_KEY: "fr"}):
        assert gettext("Statistics") == "Chiffres"
        # A string the mounted catalog says nothing about keeps its own.
        assert gettext("Documentation") == "Documentation"


def test_dates_read_day_first_in_every_language(app: Flask) -> None:
    """Every language gets the same day/month/year slashes, not CLDR's own short form."""
    import datetime

    day = datetime.datetime(2026, 9, 5, 14, 30, tzinfo=datetime.UTC)
    with app.test_request_context("/", environ_base={i18n.ENVIRON_KEY: "de"}):  # CLDR de: dd.MM.yy

        def render(src: str, **kw: object) -> str:
            return app.jinja_env.from_string(src).render(**kw)

        assert render("{{ d | dateformat('short') }}", d=day.date()) == "05/09/2026"
        assert render("{{ d | datetimeformat('short') }}", d=day) == "05/09/2026 16:30"
