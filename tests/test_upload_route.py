"""What POST /brands/<brand>/upload writes down, and what it answers.

The route is exercised on the real schema — the history rows are the point —
but not on `src.app`: importing it runs the migrations against the development
database. A Flask app holding the blueprint alone is enough, with `get_osmdb`
pointed at the throwaway database.

`BulkUpload` is the real one: in development it drives `_FakeOsmApi`, so a
success here goes through the code production runs. Only the failures are
staged, through a stand-in that reports what an unreachable OSM would.
"""

import json

import pytest
from flask import Flask
from flask_babel import Babel
from psycopg.rows import dict_row

import src.routes.brands as brands
from src.extensions import cache
from src.upload import BulkUpload


@pytest.fixture
def client(migrated_conn, monkeypatch):
    app = Flask(__name__, template_folder="website/templates")
    app.secret_key = "test"
    # The changeset comment goes through gettext.
    Babel(app)
    app.config["CACHE_TYPE"] = "SimpleCache"
    cache.init_app(app)
    app.register_blueprint(brands.brands_bp)

    monkeypatch.setattr(brands, "get_osmdb", lambda: migrated_conn)
    # The logs of a run are a production artefact, not a test one.
    monkeypatch.setattr(BulkUpload, "save_log_file", lambda self: None)
    monkeypatch.setattr(BulkUpload, "_write_osc", lambda *a, **k: None)

    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user"] = {"osm_id": 42}
            sess["token"] = {"access_token": "x"}
        yield c


def change(id, sub="75", name="Paris"):
    return {
        "id": id,
        "version": 1,
        "node_type": "node",
        "tag": {"brand": "Babylone", "brand:wikidata": "Q123", "email": "a@b.fr"},
        "members": [],
        "subdivision_code": sub,
        "subdivision_name": name,
        "atp_brand": "Babylone",
        "tags_added": ["email"],
    }


def batch(monkeypatch, changes):
    monkeypatch.setattr(brands, "get_batch", lambda wikidata: (changes, {}))


def history(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM import_history ORDER BY id").fetchall()


def subdivisions(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(
            "SELECT * FROM import_subdivisions ORDER BY subdivision_code"
        ).fetchall()


def test_a_full_success_answers_200_and_records_the_import(
    client, migrated_conn, monkeypatch
):
    batch(monkeypatch, [change(1), change(2, sub="33", name="Gironde")])

    res = client.post("/brands/Q123/upload")

    assert res.status_code == 200
    body = json.loads(res.data)
    assert "partial" not in body

    (entry,) = history(migrated_conn)
    assert entry["id"] == body["id"]
    assert entry["status"] == "success"
    assert entry["items_count"] == 2
    assert entry["osm_user_id"] == 42
    assert entry["brand_name"] == "Babylone"
    assert entry["comment"] is None

    rows = subdivisions(migrated_conn)
    assert [(r["subdivision_code"], r["subdivision_name"], r["status"]) for r in rows] == [
        ("33", "Gironde", "success"),
        ("75", "Paris", "success"),
    ]
    assert all(r["osm_changeset_id"] is not None for r in rows)


def test_a_partial_failure_answers_200_with_the_errors(
    client, migrated_conn, monkeypatch
):
    batch(monkeypatch, [change(1), change(2, sub="33", name="Gironde")])
    monkeypatch.setattr(brands, "BulkUpload", _staged(partial=True))

    res = client.post("/brands/Q123/upload")

    assert res.status_code == 200
    body = json.loads(res.data)
    assert body["partial"] is True
    assert body["errors"] == ["boom"]

    (entry,) = history(migrated_conn)
    assert entry["status"] == "partial"
    assert entry["comment"] == "boom"
    # Only what actually reached OSM is counted.
    assert entry["items_count"] == 1
    # Rows read back sorted by code: 33 is the one that failed.
    assert [r["status"] for r in subdivisions(migrated_conn)] == [
        "error_osm_api",
        "success",
    ]


def test_a_total_failure_answers_422_and_still_records_it(
    client, migrated_conn, monkeypatch
):
    batch(monkeypatch, [change(1)])
    monkeypatch.setattr(brands, "BulkUpload", _staged(partial=False))

    res = client.post("/brands/Q123/upload")

    assert res.status_code == 422
    body = json.loads(res.data)
    assert body["errors"] == ["boom"]

    (entry,) = history(migrated_conn)
    assert entry["id"] == body["id"]
    assert entry["status"] == "error"
    assert entry["items_count"] == 0
    assert [r["status"] for r in subdivisions(migrated_conn)] == ["error_osm_api"]


def test_a_brand_under_cooldown_is_refused_and_nothing_is_sent(
    client, migrated_conn, monkeypatch
):
    # A childless import row hides the whole brand for the cooldown.
    with migrated_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, osm_user_id, status)"
            " VALUES ('Q123', 42, 'cancelled')"
        )
        migrated_conn.commit()
    batch(monkeypatch, [change(1)])
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = client.post("/brands/Q123/upload")

    assert res.status_code == 403
    assert json.loads(res.data) == {"error": "Forbidden"}
    assert subdivisions(migrated_conn) == []


def test_a_batch_over_the_limit_is_refused_and_nothing_is_sent(
    client, migrated_conn, monkeypatch
):
    batch(monkeypatch, [change(i) for i in range(brands.BATCH_MAX_SIZE + 1)])
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = client.post("/brands/Q123/upload")

    assert res.status_code == 403
    assert json.loads(res.data) == {"error": "Import too large"}
    assert history(migrated_conn) == []


def test_an_anonymous_visitor_cannot_upload(client, monkeypatch):
    monkeypatch.setattr(brands, "BulkUpload", _never_called)
    with client.session_transaction() as sess:
        sess.clear()

    assert client.post("/brands/Q123/upload").status_code == 403


def _never_called(*args, **kwargs):
    raise AssertionError("the route must not reach OSM here")


def _staged(partial: bool):
    """A BulkUpload whose last subdivision fails — the whole batch if alone."""

    class Staged(BulkUpload):
        def upload(self):
            subs = self._sorted_by_subdivision()
            for i, (sub, sub_changes) in enumerate(subs.items()):
                if partial and i == 0:
                    self.changesets.append(1)
                    self.uploaded_changes.extend(sub_changes)
                    self._record(sub, sub_changes, "success", 1, None)
                else:
                    self._record(sub, sub_changes, "error_osm_api", None, "boom")
            return [("osm_api", "boom")]

    return Staged
