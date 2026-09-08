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
        "nsi_locations": ["fr", "150", "eu", "001"],
        "nsi_writable_tags": ["brand:wikidata"],
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
    return db_kwargs


@pytest.fixture
def migrated_conn(_migrated, db_kwargs):
    """A connection on the migrated schema, emptied before each test."""
    with psycopg.connect(**db_kwargs) as c:
        tables = c.execute(
            "SELECT string_agg(quote_ident(tablename), ', ') FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
        ).fetchone()[0]
        if tables:
            c.execute(f"TRUNCATE {tables} CASCADE")
        c.commit()
        yield c
