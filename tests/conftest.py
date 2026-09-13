"""A configuration for the tests, written before anything imports the settings.

No country file ships with the product, so the tests cannot borrow one — they
give themselves a plausible country instead. It is written at import time
rather than in a fixture because `src.pipeline.constants` reads the settings
while it is being imported, which happens at collection.
"""

import json
import os
import tempfile

CONFIG = {
    "country": {
        "territory_codes": ["fr", "mq"],
        "locales": ["fr"],
        "timezone": "Europe/Paris",
        "geofabrik": ["europe/france"],
        "admin_level": 6,
        "admin_level_max": 8,
        "match_radius_m": 500,
        "calling_codes": ["33", "262", "508", "590", "594", "596",
                          "681", "687", "689"],
        "trunk_prefix": "0",
        "nsi_locations": ["fr", "fx", "gp", "mq", "gf", "re", "yt", "pm", "bl",
                          "mf", "nc", "pf", "wf", "tf", "001", "150", "europe", "eu"],
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

_path = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", prefix="atp2osm-test-", delete=False
)
json.dump(CONFIG, _path)
_path.close()

# The real database settings win when they are given: the tests that need a
# live PostGIS read them from the environment, as they always did.
os.environ.setdefault("ATP2OSM_CONFIG", _path.name)
for _name, _value in _SECRETS.items():
    os.environ.setdefault(_name, _value)


# --- The database the tests run on ----------------------------------------
#
# Never the development one: its content is nobody's guarantee — an
# interrupted pipeline leaves rows behind, and a test reading them fails for
# reasons that have nothing to do with it. Everything below builds a throwaway
# database instead, dropped when the session ends.

import psycopg  # noqa: E402
import pytest  # noqa: E402

TEST_DB = "atp2osm_test"

# A test that does not run controls nothing, so there is no skip here: an
# unreachable database is an error. `podman-compose up -d` is a prerequisite
# of the suite, and a CI that lost its service must say so loudly instead of
# reporting green on a third of the tests.


@pytest.fixture(scope="session")
def db_kwargs():
    """Connection kwargs to an empty throwaway database with PostGIS enabled.

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
        admin = get_database().connect_kwargs
        with psycopg.connect(**admin, autocommit=True) as c:
            c.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')
            c.execute(f'CREATE DATABASE "{TEST_DB}"')
        kwargs = {**admin, "dbname": TEST_DB}
        with psycopg.connect(**kwargs) as c:
            c.execute("CREATE EXTENSION IF NOT EXISTS postgis")
            c.commit()
    except (psycopg.Error, ConfigError) as exc:
        pytest.fail(
            f"cannot build the test database: {exc}\n"
            "Start it with `podman-compose up -d`.",
            pytrace=False,
        )

    yield kwargs

    with psycopg.connect(**admin, autocommit=True) as c:
        c.execute(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)')


@pytest.fixture(scope="session")
def _migrated(db_kwargs):
    """Run the migrations once — the schema of the tests is the real one."""
    from src.migrate import run_migrations

    with psycopg.connect(**db_kwargs) as c:
        run_migrations(c)
        # The pipeline's tables the site reads, empty: the cooldown SQL joins
        # them, and a migration never creates them.
        c.execute("""
            CREATE TABLE atp_places (id TEXT, spider_id TEXT, brand_wikidata TEXT, brand TEXT);
            CREATE TABLE atp_spiders (spider TEXT, filename TEXT, errors INT8, features INT8,
                                      elapsed_time FLOAT8, updated_at TIMESTAMPTZ);
        """)
        c.commit()
    return db_kwargs


@pytest.fixture
def migrated_conn(_migrated, db_kwargs):
    """A connection on the migrated schema, emptied before each test."""
    with psycopg.connect(**db_kwargs) as c:
        tables = c.execute(
            "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables"
            " WHERE schemaname = 'public'"
            # schema_migrations is the record of what has been applied, and
            # spatial_ref_sys is PostGIS's own catalogue: emptying it leaves a
            # database where no geometry can be given an SRID.
            " AND tablename NOT IN ('schema_migrations', 'spatial_ref_sys')"
        ).fetchone()[0]
        if tables:
            c.execute(f"TRUNCATE {tables} CASCADE")
        c.commit()
        yield c


# --- No test reaches the network -------------------------------------------
#
# The OSM API, Geofabrik, ATP and the npm registry are never called from a
# test: a test that depends on them is green or red on their mood, not on the
# code. Every HTTP request goes through requests.Session.request — this stops
# it there. A test that wants a response stages one with monkeypatch on the
# function that would have asked.


class NetworkAccess(AssertionError):
    """A test tried to reach the network."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def refused(self, method, url, *args, **kwargs):
        raise NetworkAccess(f"the test suite never reaches the network: {method} {url}")

    monkeypatch.setattr(requests.Session, "request", refused)


# --- The site, on the throwaway database -------------------------------------
#
# Never `src.app`: importing it runs the migrations against the development
# database. This builds the same app — the real blueprints, templates, filters
# and globals — with every request connecting to the test database, exactly
# as production connects to its own: a connection per request, closed at the
# end of it. The tests read the result on `migrated_conn`.


@pytest.fixture
def web_app(migrated_conn, db_kwargs, monkeypatch):
    from flask import Flask

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

    class _TestDatabase:
        connect_kwargs = db_kwargs

    monkeypatch.setattr(src.db, "get_database", lambda: _TestDatabase)

    settings = get_settings()
    app = Flask("atp2osm-test", template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
    app.secret_key = "test"
    app.config["CACHE_TYPE"] = "SimpleCache"
    cache.init_app(app)
    # No translated path: the pages answer on their bare URL, no language
    # redirect to follow.
    i18n.init_app(app, settings.country.locales, (), settings.country.timezone)
    templating.init_app(app, settings)
    for blueprint in (auth_bp, brands_bp, spiders_bp, export_bp, history_bp,
                      misc_bp, stats_bp, todo_bp):
        app.register_blueprint(blueprint)
    app.teardown_appcontext(src.db.teardown_osmdb)
    return app


@pytest.fixture
def contributor(web_app):
    """A client signed in as OSM user 42."""
    with web_app.test_client() as client:
        with client.session_transaction() as sess:
            # What oauth_callback stores: the pages read the name too.
            sess["user"] = {"osm_id": 42, "name": "reviewer"}
            sess["token"] = {"access_token": "x"}
        yield client
