"""Accessibility of every page, checked by pa11y-ci (axe + HTML_CodeSniffer).

The rules need a browser on a rendered page, so this is the one test that
runs the real `src.app` as a server. It serves the throwaway database, seeded
with a world small enough to read and complete enough to reach every screen:
one brand with a match left to integrate, so /validate and /confirm render
their forms, and a history with a success, a failure and a partial import.

Pages behind OSM login are visited with a forged session cookie: the server
gets the test SECRET_KEY, so a cookie signed with it here is a valid login.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

import psycopg
import pytest
from flask import Flask
from flask.sessions import SecureCookieSessionInterface

from src.phone import ensure_normalize_phone
from src.pipeline.atp2osm import _mv_places_brand_sql, _mv_places_spider_sql
from src.pipeline.osm import _mv_places_sql
from tests.conftest import CONFIG, TEST_DB

QID = "Q999001"
SECRET = "test"

pytestmark = pytest.mark.a11y

PUBLIC = ["/", "/fr/", "/brands", "/spiders", "/history", "/stats", "/todo", "/docs", "/about"]
LOGGED_IN = [
    f"/brands/{QID}/validate",
    f"/brands/{QID}/confirm",
    f"/brands/{QID}/rejected",
]
# Filled by the seed with the ids it created: the history detail pages.
HISTORY = []


def seed(conn):
    """The osm2pgsql and ATP tables, shaped like the pipeline leaves them."""
    ensure_normalize_phone(conn)
    # `_migrated` leaves empty stubs of the pipeline's tables; the real shape
    # is built here.
    conn.execute("""
        DROP TABLE IF EXISTS atp_places, atp_spiders;
        CREATE TABLE subdivisions (
            area_id SERIAL, osm_id INT8 NOT NULL, ref TEXT, name TEXT NOT NULL,
            admin_level INT NOT NULL, geom GEOMETRY(Geometry, 4326) NOT NULL
        );
        INSERT INTO subdivisions (osm_id, ref, name, admin_level, geom) VALUES
            (1, 'FR', 'France', 2, ST_MakeEnvelope(-5, 41, 10, 52, 4326)),
            (2, '75', 'Paris', 6, ST_MakeEnvelope(2.2, 48.8, 2.5, 48.9, 4326));
        CREATE TABLE subdivision_parts AS
            SELECT osm_id, ref, name, admin_level, geom FROM subdivisions;

        CREATE TABLE points (
            node_id INT8 PRIMARY KEY, tags JSONB, geom GEOMETRY(Point, 4326) NOT NULL,
            version INT, osm_timestamp INT8
        );
        INSERT INTO points VALUES
            (101, '{"name": "Babylone", "shop": "clothes", "brand": "Babylone",
                    "brand:wikidata": "Q999001", "addr:postcode": "75001"}',
             ST_SetSRID(ST_Point(2.35, 48.85), 4326), 3, 1700000000);

        CREATE TABLE polygons (
            area_id INT8 PRIMARY KEY, osm_type TEXT NOT NULL, tags JSONB, members JSONB,
            geom GEOMETRY(Geometry, 4326) NOT NULL, version INT, osm_timestamp INT8
        );
        INSERT INTO polygons VALUES
            (201, 'W', '{"name": "Babylone", "shop": "clothes", "brand": "Babylone",
                         "brand:wikidata": "Q999001", "addr:postcode": "75002",
                         "phone": "+33 1 23 45 67 89"}',
             NULL, ST_MakeEnvelope(2.36, 48.86, 2.361, 48.861, 4326), 2, 1700000000);

        CREATE TABLE atp_places (
            id TEXT PRIMARY KEY, country TEXT, city TEXT, postcode TEXT,
            brand_wikidata TEXT, brand TEXT, name TEXT, opening_hours TEXT,
            website TEXT, phone TEXT, email TEXT, end_date TEXT, spider_id TEXT,
            source_type TEXT, source_uri TEXT, geom TEXT,
            subdivision_code TEXT, subdivision_name TEXT
        );
        INSERT INTO atp_places VALUES
            ('atp-1', 'FR', 'Paris', '75001', 'Q999001', 'Babylone', 'Babylone Louvre',
             'Mo-Sa 10:00-19:00', 'https://babylone.example', '+33 1 98 76 54 32',
             'louvre@babylone.example', NULL, 'babylone_fr', NULL,
             'https://babylone.example/stores/1',
             '{"type": "Point", "coordinates": [2.3501, 48.8501]}', '75', 'Paris'),
            ('atp-2', 'FR', 'Paris', '75002', 'Q999001', 'Babylone', 'Babylone Bourse',
             'Mo-Sa 10:00-19:00', 'https://babylone.example', '+33 1 23 45 67 89',
             'bourse@babylone.example', NULL, 'babylone_fr', NULL,
             'https://babylone.example/stores/2',
             '{"type": "Point", "coordinates": [2.3605, 48.8605]}', '75', 'Paris');

        INSERT INTO nsi_brands (brand_wikidata, brand, name, primary_key, primary_value, tags)
        VALUES ('Q999001', 'Babylone', 'Babylone', 'shop', 'clothes',
                '{"brand:wikidata": "Q999001", "shop": "clothes", "clothes": "women"}');

        INSERT INTO data_imports (type, date, status, comment) VALUES
            ('osm', NOW() - INTERVAL '1 day', 'success', NULL),
            ('atp', NOW() - INTERVAL '1 day', 'success', NULL),
            ('nsi', NOW() - INTERVAL '1 day', 'success', 'v6.0.20260901'),
            ('pipeline', NOW() - INTERVAL '1 day', 'success', NULL);

        CREATE TABLE atp_spiders (
            spider TEXT, filename TEXT, errors INT8, features INT8,
            elapsed_time FLOAT8, updated_at TIMESTAMPTZ
        );
        INSERT INTO atp_spiders VALUES
            ('babylone_fr', 'locations/spiders/babylone_fr.py', 0, 2, 1.5,
             NOW() - INTERVAL '3 days'),
            ('broken_fr', 'locations/spiders/broken_fr.py', 3, 0, 0.1, NULL);

        INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id, estimation)
        VALUES ('Q999002', 'Missing Brand', 42, 120);
    """)
    rows = conn.execute("""
        INSERT INTO import_history
            (brand_wikidata, brand_name, osm_user_id, import_date, status,
             comment, items_count, tags_count)
        VALUES
            ('Q999003', 'Old Brand', 42, NOW() - INTERVAL '2 years', 'success',
             NULL, 12, '{"phone": 12, "website": 4}'),
            ('Q999004', 'Broken Brand', 43, NOW() - INTERVAL '1 year', 'error',
             'OSM API unreachable', 0, NULL),
            ('Q999005', 'Half Brand', 42, NOW() - INTERVAL '1 year', 'partial',
             NULL, 7, '{"email": 7}'),
            ('Q999006', 'Reported Brand', 44, NOW() - INTERVAL '1 year', 'cancelled',
             '{"reason": "wrong_brand", "comment": "Not the same shops"}', 0, NULL)
        RETURNING id
    """).fetchall()
    HISTORY.extend(f"/history/{r[0]}" for r in rows)
    conn.execute("""
        INSERT INTO import_subdivisions
            (import_id, subdivision_code, subdivision_name, items_count,
             osm_changeset_id, status, comment)
        VALUES
            (%s, '75', 'Paris', 12, 1000001, 'success', NULL),
            (%s, '75', 'Paris', 7, 1000002, 'success', NULL),
            (%s, '33', 'Gironde', 5, NULL, 'error_osm_api', 'timeout')
    """, (rows[0][0], rows[2][0], rows[2][0]))
    conn.execute(_mv_places_sql())
    conn.execute(_mv_places_brand_sql())
    conn.execute(_mv_places_spider_sql())
    conn.commit()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(_migrated, tmp_path_factory):
    with psycopg.connect(**_migrated) as conn:
        seed(conn)
    port = free_port()
    config = json.loads(json.dumps(CONFIG))
    config["app"]["db"]["name"] = TEST_DB
    config["app"]["base_url"] = f"http://127.0.0.1:{port}"
    # A closed port: the user names come back empty at once, no OSM call.
    config["app"]["osm_api_host"] = "http://127.0.0.1:9"
    config_path = tmp_path_factory.mktemp("a11y") / "config.json"
    config_path.write_text(json.dumps(config))

    env = {**os.environ, "ATP2OSM_CONFIG": str(config_path), "SECRET_KEY": SECRET}
    # A file, not a pipe: the request log would fill a pipe nobody reads and
    # freeze the server mid-run.
    log = open(config_path.with_name("server.log"), "w+b")
    proc = subprocess.Popen(
        [sys.executable, "-m", "flask", "--app", "src/app.py", "run", "--port", str(port)],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    for _ in range(60):
        if proc.poll() is not None:
            log.seek(0)
            pytest.fail("the server died:\n" + log.read().decode())
        try:
            urlopen(url + "/robots.txt", timeout=1)
            break
        except OSError:
            time.sleep(0.5)
    else:
        proc.kill()
        pytest.fail("the server never answered")
    yield url, env
    proc.kill()
    proc.wait()
    # The pipeline's objects are not part of the migrated schema: a test after
    # this one may build a table of the same name, so they leave with the
    # server. `migrated_conn` truncates tables and knows nothing of views.
    with psycopg.connect(**_migrated) as conn:
        conn.execute("""
            DROP MATERIALIZED VIEW IF EXISTS mv_places_brand;
            DROP MATERIALIZED VIEW IF EXISTS mv_places_spider;
            DROP MATERIALIZED VIEW IF EXISTS mv_places;
            DROP TABLE IF EXISTS atp_places, atp_spiders, points, polygons,
                                 subdivisions, subdivision_parts
        """)
        conn.commit()


def test_every_get_route_is_visited(server):
    """A page nobody listed is a page nobody checks: a new route lands here
    until it is added to the pages above or to the exclusions below."""
    _, env = server
    out = subprocess.run(
        [sys.executable, "-m", "flask", "--app", "src/app.py", "routes"],
        env=env, capture_output=True, text=True, check=True,
    ).stdout
    rules = [
        line.split()[-1]
        for line in out.splitlines()[2:]
        if "GET" in line and not line.startswith("static")
    ]
    visited = set(PUBLIC + LOGGED_IN) | {"/history/<int:entry_id>"}
    # Not pages: files, JSON, the OAuth callback, the language-prefixed twins.
    excluded = {
        "/favicon.ico", "/robots.txt", "/sitemap.xml", "/llms.txt",
        "/google1387dd4d6e23b123.html", "/staticmap/<long>/<lat>",
        "/api/export/departements.<fmt>", "/api/export/<dataset>.<fmt>", "/api/stats.json",
        "/todo/check", "/oauth-callback",
    }
    forgotten = [
        r for r in rules
        if not r.startswith("/<lang>")
        and r.replace("<brand_wikidata>", QID) not in visited
        and r not in excluded
    ]
    assert not forgotten
    assert "/brands/<brand_wikidata>/validate" in rules  # the parsing above reads something


def login_cookie():
    app = Flask(__name__)
    app.secret_key = SECRET
    session = {"user": {"osm_id": 42, "name": "Tester"},
               "token": {"access_token": "x"}}
    value = SecureCookieSessionInterface().get_signing_serializer(app).dumps(session)
    return f"session={value}"


@pytest.mark.skipif(shutil.which("npx") is None, reason="pa11y-ci needs node")
def test_every_page_passes_wcag_2_aa(server, tmp_path):
    server, _ = server
    cookie = login_cookie()
    urls = [{"url": server + p} for p in PUBLIC + HISTORY]
    urls += [{"url": server + p, "headers": {"Cookie": cookie}} for p in LOGGED_IN]
    # Every page twice: the browser starts light, the switch turns it dark.
    # The query string keeps pa11y-ci from folding the two runs into one.
    urls += [
        {**u, "url": u["url"] + "?theme=dark",
         "actions": ["click element #theme-toggle",
                     "wait for element html[data-theme=dark] to be added"]}
        for u in urls
    ]
    # A 500 renders an error page that passes or fails on its own merits: make
    # sure every page is the one we meant to check.
    for u in urls:
        assert urlopen(Request(u["url"], headers=u.get("headers", {}))).status == 200, u
    config = tmp_path / "pa11yci.json"
    config.write_text(json.dumps({
        "defaults": {
            "standard": "WCAG2AA",
            "runners": ["axe", "htmlcs"],
            # What axe cannot decide (a text over a gradient, a button with a
            # noise texture) is a warning to read, not a failure.
            "levelCapWhenNeedsReview": "warning",
            # Tailwind runs from its CDN script and writes the utility classes
            # after the page is painted: audited too early, a page has no
            # colours yet and every contrast fails at random.
            "wait": 500,
            # AppArmor forbids Chromium's own sandbox on stock Ubuntu.
            "chromeLaunchConfig": {"args": ["--no-sandbox"]},
        },
        "urls": urls,
    }))
    result = subprocess.run(
        ["npx", "-y", "pa11y-ci", "-c", str(config)],
        capture_output=True, text=True, timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
