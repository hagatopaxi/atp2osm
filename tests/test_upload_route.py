"""What POST /brands/<brand>/upload writes down, and what it answers.

The route is exercised on the real schema — the history rows are the point —
through the `contributor` client of conftest: the site's blueprints on the
throwaway database, never `src.app`.

`BulkUpload` is the real one: in development it drives `FakeOsmApi`, so a
success here goes through the code production runs. Only the failures are
staged, through a stand-in that reports what an unreachable OSM would.
"""

import json
from collections.abc import Iterator
from typing import Never

import pytest
from flask.testing import FlaskClient
from psycopg.rows import DictRow, dict_row

from src.matching import WAVES_BY_NUMBER, Change, Wave
from src.routes import brands
from src.upload import BulkUpload
from tests.conftest import Connection, make_change, reviewed


@pytest.fixture
def client(
    contributor: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FlaskClient]:
    # The logs of a run are a production artefact, not a test one.
    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(BulkUpload, "save_log_file", nothing)
    monkeypatch.setattr(BulkUpload, "_write_osc", nothing)
    # The cooldowns read the brand view, empty here: the batch is staged.
    with migrated_conn.cursor() as cur:
        cur.execute("""
            DROP TABLE IF EXISTS mv_places_brand;
            CREATE TABLE mv_places_brand (brand TEXT, brand_wikidata TEXT, subdivision_code TEXT,
                                          wave SMALLINT, total BIGINT);
        """)
    migrated_conn.commit()
    yield contributor
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand")
    migrated_conn.commit()


def change(osm_id: int, sub: str = "75", name: str = "Paris") -> Change:
    return make_change(
        id=osm_id,
        tag={"brand": "Babylone", "brand:wikidata": "Q123", "email": "a@b.fr"},
        members=[],
        subdivision_code=sub,
        subdivision_name=name,
    )


def batch(
    monkeypatch: pytest.MonkeyPatch, changes: list[Change], wave: Wave = WAVES_BY_NUMBER[1]
) -> None:
    def get_batch(_wikidata: str) -> brands.Batch:
        return brands.Batch(changes, [], wave, [], frozenset(), replayed=False)

    monkeypatch.setattr(brands, "get_batch", get_batch)


def history(conn: Connection) -> list[DictRow]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM import_history ORDER BY id").fetchall()


def subdivisions(conn: Connection) -> list[DictRow]:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM import_subdivisions ORDER BY subdivision_code").fetchall()


def test_a_full_success_answers_200_and_records_the_import(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(1), change(2, sub="33", name="Gironde")])

    res = client.post(f"/brands/Q123/upload?batch={reviewed(client, 'Q123')}")

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
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(1), change(2, sub="33", name="Gironde")])
    monkeypatch.setattr(brands, "BulkUpload", _staged(partial=True))

    res = client.post(f"/brands/Q123/upload?batch={reviewed(client, 'Q123')}")

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
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(1)])
    monkeypatch.setattr(brands, "BulkUpload", _staged(partial=False))

    res = client.post(f"/brands/Q123/upload?batch={reviewed(client, 'Q123')}")

    assert res.status_code == 422
    body = json.loads(res.data)
    assert body["errors"] == ["boom"]

    (entry,) = history(migrated_conn)
    assert entry["id"] == body["id"]
    assert entry["status"] == "error"
    assert entry["items_count"] == 0
    assert [r["status"] for r in subdivisions(migrated_conn)] == ["error_osm_api"]


def test_a_brand_under_cooldown_is_refused_and_nothing_is_sent(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A childless import row hides the whole brand for the cooldown.
    with migrated_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, osm_user_id, status)"
            " VALUES ('Q123', 42, 'cancelled')"
        )
        migrated_conn.commit()
    batch(monkeypatch, [change(1)])
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = client.post(f"/brands/Q123/upload?batch={reviewed(client, 'Q123')}")

    assert res.status_code == 403
    assert json.loads(res.data) == {"errors": ["Brand under cooldown"]}
    assert subdivisions(migrated_conn) == []


def test_a_batch_over_the_limit_is_refused_and_nothing_is_sent(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(i) for i in range(WAVES_BY_NUMBER[1].batch_size + 1)])
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = client.post(f"/brands/Q123/upload?batch={reviewed(client, 'Q123')}")

    assert res.status_code == 403
    assert json.loads(res.data) == {"errors": ["Import too large"]}
    assert history(migrated_conn) == []


def test_the_batch_sent_is_the_one_reviewed(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The matches moved between the review and the upload — a refresh, an
    integration of another brand: what goes out is still what was read.
    """
    batch(monkeypatch, [change(1)])
    batch_id = reviewed(client, "Q123")
    batch(monkeypatch, [change(2, sub="33", name="Gironde")])

    assert client.post(f"/brands/Q123/upload?batch={batch_id}").status_code == 200
    assert [r["subdivision_code"] for r in subdivisions(migrated_conn)] == ["75"]


@pytest.mark.parametrize("batch_id", ["", "lost-to-a-deploy"])
def test_a_batch_the_cache_does_not_hold_is_refused(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch, batch_id: str
) -> None:
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = client.post(f"/brands/Q123/upload?batch={batch_id}")

    assert res.status_code == 409
    assert history(migrated_conn) == []


def test_a_batch_goes_out_once(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(1)])
    batch_id = reviewed(client, "Q123")
    assert client.post(f"/brands/Q123/upload?batch={batch_id}").status_code == 200

    monkeypatch.setattr(brands, "BulkUpload", _never_called)
    assert client.post(f"/brands/Q123/upload?batch={batch_id}").status_code == 409
    assert len(history(migrated_conn)) == 1


def test_a_batch_is_its_reviewers_own(
    client: FlaskClient, migrated_conn: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch(monkeypatch, [change(1)])
    batch_id = reviewed(client, "Q123")
    with client.session_transaction() as sess:
        sess["user"] = {"osm_id": 43, "name": "someone else"}
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    assert client.post(f"/brands/Q123/upload?batch={batch_id}").status_code == 409
    assert history(migrated_conn) == []


def test_an_anonymous_visitor_cannot_upload(
    client: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(brands, "BulkUpload", _never_called)
    with client.session_transaction() as sess:
        sess.clear()

    assert client.post("/brands/Q123/upload?batch=any").status_code == 403


def _never_called(*_args: object, **_kwargs: object) -> Never:
    raise AssertionError("the route must not reach OSM here")


def _staged(*, partial: bool) -> type[BulkUpload]:
    """A BulkUpload whose last subdivision fails — the whole batch if alone."""

    class Staged(BulkUpload):
        def upload(self) -> list[tuple[str, str]]:
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
