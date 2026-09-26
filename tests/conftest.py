"""A configuration for the tests, written before anything imports the settings.

No country file ships with the product, so the tests cannot borrow one — they
give themselves a plausible country instead. It is written at import time
rather than in a fixture because `src.pipeline.constants` reads the settings
while it is being imported, which happens at collection.
"""

import json
import os
import tempfile
from typing import Any

CONFIG: dict[str, Any] = {
    "country": {
        "territory_codes": ["fr", "mq"],
        "locales": ["fr"],
        "timezone": "Europe/Paris",
        "geofabrik": ["europe/france"],
        "admin_level": 6,
        "admin_level_max": 8,
        "match_radius_m": 500,
        "calling_codes": ["33", "262", "508", "590", "594", "596", "681", "687", "689"],
        "trunk_prefix": "0",
        "nsi_locations": [
            "fr",
            "fx",
            "gp",
            "mq",
            "gf",
            "re",
            "yt",
            "pm",
            "bl",
            "mf",
            "nc",
            "pf",
            "wf",
            "tf",
            "001",
            "150",
            "europe",
            "eu",
        ],
        # The measured French list: the NSI tests assert on what it keeps.
        "nsi_writable_tags": [
            "brand:wikidata",
            "shop",
            "amenity",
            "office",
            "tourism",
            "leisure",
            "healthcare",
            "craft",
            "network:wikidata",
            "operator:wikidata",
            "official_name",
            "alt_name",
            "brand:short",
            "name:en",
            "brand:en",
            "name:fr",
            "brand:fr",
            "government",
            "drive_through",
            "healthcare:speciality",
            "service:vehicle:glass",
            "delivery",
            "access",
            "self_service",
            "clothes",
            "takeaway",
            "operator:type",
        ],
    },
    "app": {
        "env": "DEVELOPMENT",
        "base_url": "http://localhost:5000",
        "osm_api_host": "https://api.openstreetmap.org",
        "db": {"name": "o2p", "user": "o2p", "host": "127.0.0.1", "port": 5434},
    },
}

_SECRETS = {
    "OSM_DB_PASSWORD": "test",
    "OSM_OAUTH_CLIENT_ID": "test",
    "OSM_OAUTH_CLIENT_SECRET": "test",
    "SECRET_KEY": "test",
}

with tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", prefix="atp2osm-test-", delete=False
) as _config_file:
    json.dump(CONFIG, _config_file)

# The real database settings win when they are given: the tests that need a
# live PostGIS read them from the environment, as they always did.
os.environ.setdefault("ATP2OSM_CONFIG", _config_file.name)
for _name, _value in _SECRETS.items():
    os.environ.setdefault(_name, _value)


# --- The database the tests run on ----------------------------------------
#
# Never the development one: its content is nobody's guarantee — an
# interrupted pipeline leaves rows behind, and a test reading them fails for
# reasons that have nothing to do with it. Everything below builds a throwaway
# database instead, dropped when the session ends.

import dataclasses  # noqa: E402
import importlib.util  # noqa: E402
import pathlib  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from types import ModuleType  # noqa: E402
from typing import Never, TypeVar, cast  # noqa: E402

import psycopg  # noqa: E402
import pytest  # noqa: E402
from flask import Flask  # noqa: E402
from flask.testing import FlaskClient  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import TupleRow  # noqa: E402

from src.config import Database  # noqa: E402
from src.db import code_sql  # noqa: E402
from src.matching import Change  # noqa: E402

# What the fixtures hand out: a connection on the throwaway database.
Connection = psycopg.Connection[TupleRow]

T = TypeVar("T")

ROOT = pathlib.Path(__file__).parent.parent


def load_module(path: pathlib.Path) -> ModuleType:
    """A file loaded as a module — a migration or a script, which no package names."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None, path
    assert spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def one(row: T | None) -> T:
    """The row a query returned — a test knows when there is one."""
    assert row is not None, "no row"
    return row


def make_change(**overrides: Any) -> Change:  # noqa: ANN401 — whichever fields the test sets
    """A complete proposal, as apply_on_node builds it, with what the test sets."""
    change: Change = {
        "id": 1,
        "node_type": "node",
        "version": 1,
        "tag": {},
        "members": None,
        "lon": 2.35,
        "lat": 48.85,
        "atp_brand": "Babylone",
        "atp_id": "atp-1",
        "spider_id": "babylone_fr",
        "source_uri": "https://babylone.fr",
        "source_type": "spider",
        "postcode": "75001",
        "old_tag": {},
        "osm_timestamp": None,
        "brand_wikidata_source": None,
        "subdivision_code": "75",
        "subdivision_name": "Paris",
    }
    return cast("Change", {**change, **overrides})


# Named after the process: two suites running at once — one per worktree —
# must not drop each other's database from under them.
TEST_DB = f"atp2osm_test_{os.getpid()}"

# A test that does not run controls nothing, so there is no skip here: an
# unreachable database is an error. `podman-compose up -d` is a prerequisite
# of the suite, and a CI that lost its service must say so loudly instead of
# reporting green on a third of the tests.


def _drop_orphans(admin_conn: Connection) -> None:
    """Drop the databases of suites that died before their teardown."""
    names: list[tuple[str]] = admin_conn.execute(
        "SELECT datname FROM pg_database WHERE datname LIKE 'atp2osm_test_%'"
    ).fetchall()
    for (name,) in names:
        # atp2osm_test_<pid>, or a test's own atp2osm_test_<pid>_<suffix>.
        pid = next((int(p) for p in name.split("_") if p.isdigit()), None)
        if pid is None:
            continue
        try:
            os.kill(pid, 0)  # alive: its suite is still running
        except (OSError, ProcessLookupError):
            admin_conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture(scope="session")
def test_db() -> Iterator[Database]:
    """The settings of an empty throwaway database with PostGIS enabled.

    The single place that decides what a missing database means. A test
    needing tables either asks for `migrated_conn` or creates its own schema
    in here.
    """
    from src.config import ConfigError, get_database

    # Every way the host can turn out unable to hold the database lands here:
    # no server listening, a wrong password, a role without CREATEDB, a server
    # without PostGIS, a leftover database somebody is connected to. Only
    # psycopg.Error is caught, so a broken *test* still fails — the migrations
    # run in `_migrated`, apart.
    try:
        admin = get_database()
        with psycopg.connect(admin.conninfo, autocommit=True) as c:
            _drop_orphans(c)
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
        db = dataclasses.replace(admin, name=TEST_DB)
        with psycopg.connect(db.conninfo) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            c.commit()
    except (psycopg.Error, ConfigError) as exc:
        pytest.fail(
            f"cannot build the test database: {exc}\nStart it with `podman-compose up -d`.",
            pytrace=False,
        )

    yield db

    with psycopg.connect(admin.conninfo, autocommit=True) as c:
        c.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB))
        )


@pytest.fixture(scope="session")
def _migrated(test_db: Database) -> Database:
    """Run the migrations once — the schema of the tests is the real one."""
    from src.migrate import run_migrations

    with psycopg.connect(test_db.conninfo) as c:
        run_migrations(c)
        # The pipeline's tables the site reads, empty: the cooldown SQL joins
        # them, and a migration never creates them. Dropped first: a test on
        # `db_kwargs` alone may have built its own before this ran.
        c.execute("""
            DROP TABLE IF EXISTS atp_places, atp_spiders;
            CREATE TABLE atp_places (id TEXT, spider_id TEXT, brand_wikidata TEXT, brand TEXT);
            CREATE TABLE atp_spiders (spider TEXT, filename TEXT, errors INT8, features INT8,
                                      elapsed_time FLOAT8, updated_at TIMESTAMPTZ, log_url TEXT);
        """)
        c.commit()
    return test_db


@pytest.fixture
def migrated_conn(_migrated: Database) -> Iterator[Connection]:
    """A connection on the migrated schema, emptied before each test."""
    with psycopg.connect(_migrated.conninfo) as c:
        found = c.execute(
            "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables"
            " WHERE schemaname = 'public'"
            # schema_migrations is the record of what has been applied, and
            # spatial_ref_sys is PostGIS's own catalogue: emptying it leaves a
            # database where no geometry can be given an SRID.
            " AND tablename NOT IN ('schema_migrations', 'spatial_ref_sys')"
        ).fetchone()
        tables: str | None = found[0] if found else None
        if tables:
            c.execute(code_sql(f"TRUNCATE {tables} CASCADE"))
        c.commit()
        yield c


# --- No test reaches the network -------------------------------------------
#
# The OSM API, Geofabrik, ATP and the npm registry are never called from a
# test: a test that depends on them is green or red on their mood, not on the
# code. Every HTTP request goes through requests.Session.request — this stops
# it there. A test that wants a response stages one with monkeypatch on the
# function that would have asked.


class NetworkAccessError(AssertionError):
    """A test tried to reach the network."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def refused(_self: object, method: str, url: str, *_args: object, **_kwargs: object) -> Never:
        raise NetworkAccessError(f"the test suite never reaches the network: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", refused)


# --- The site, on the throwaway database -------------------------------------
#
# Never `src.app`: importing it runs the migrations against the development
# database. This builds the same app — the real blueprints, templates, filters
# and globals — with every request connecting to the test database, exactly
# as production connects to its own: a connection per request, closed at the
# end of it. The tests read the result on `migrated_conn`.


@pytest.fixture
def web_app(migrated_conn: Connection, test_db: Database, monkeypatch: pytest.MonkeyPatch) -> Flask:
    import src.db
    from src import i18n, templating
    from src.config import STATIC_DIR, TEMPLATE_DIR, get_settings
    from src.extensions import cache
    from src.routes.auth import auth_bp
    from src.routes.brands import brands_bp
    from src.routes.export import export_bp
    from src.routes.history import history_bp
    from src.routes.misc import misc_bp
    from src.routes.spiders import spiders_bp
    from src.routes.stats import stats_bp
    from src.routes.todo import todo_bp

    monkeypatch.setattr(src.db, "get_database", lambda: test_db)

    settings = get_settings()
    app = Flask("atp2osm-test", template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
    app.secret_key = "test"
    app.config["CACHE_TYPE"] = "SimpleCache"
    cache.init_app(app)  # pyright: ignore[reportUnknownMemberType] — see src/extensions.py
    # No translated path: the pages answer on their bare URL, no language
    # redirect to follow.
    i18n.init_app(app, settings.country.locales, (), settings.country.timezone)
    templating.init_app(app, settings)
    for blueprint in (
        auth_bp,
        brands_bp,
        spiders_bp,
        export_bp,
        history_bp,
        misc_bp,
        stats_bp,
        todo_bp,
    ):
        app.register_blueprint(blueprint)
    app.teardown_appcontext(src.db.teardown_osmdb)
    return app


@pytest.fixture
def contributor(web_app: Flask) -> Iterator[FlaskClient]:
    """A client signed in as OSM user 42."""
    with web_app.test_client() as client:
        with client.session_transaction() as sess:
            # What oauth_callback stores: the pages read the name too.
            sess["user"] = {"osm_id": 42, "name": "reviewer"}
            sess["token"] = {"access_token": "x"}
        yield client


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wave 2's guard, as production runs it. It stands aside in development —
    the dev API server holds none of the objects — so a test exercising it
    takes this fixture.
    """
    from dataclasses import replace

    from src import osm_history
    from src.config import get_settings

    settings = replace(get_settings(), env="PRODUCTION")
    monkeypatch.setattr(osm_history, "get_settings", lambda: settings)
