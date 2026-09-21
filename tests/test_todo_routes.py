"""The list of brands missing from ATP: what /todo shows and what its API
writes. On the real schema, through the site's blueprints (conftest).
"""

from collections.abc import Iterable, Iterator
from typing import Any

import pytest
from flask import Flask, template_rendered
from flask.testing import FlaskClient
from psycopg.rows import DictRow, dict_row

from src.routes import todo
from tests.conftest import Connection, one


def _named_users(ids: Iterable[int]) -> dict[int, str]:
    return {uid: f"user-{uid}" for uid in ids}


@pytest.fixture(autouse=True)
def _no_user_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # The names come from the OSM API, which is not this page's contract.
    monkeypatch.setattr(todo, "fetch_osm_users", _named_users)


@pytest.fixture
def rendered(web_app: Flask) -> Iterator[list[dict[str, Any]]]:
    contexts: list[dict[str, Any]] = []

    def record(_sender: Flask, *, context: dict[str, Any], **_extra: Any) -> None:  # noqa: ANN401 — the signal's payload
        contexts.append(context)

    template_rendered.connect(record, web_app)
    yield contexts
    template_rendered.disconnect(record, web_app)


def add(
    conn: Connection,
    name: str,
    wikidata: str | None = None,
    user: int = 42,
    estimation: int | None = None,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id, estimation)"
            " VALUES (%s, %s, %s, %s) RETURNING id",
            (wikidata, name, user, estimation),
        )
        entry_id = one(cur.fetchone())[0]
    conn.commit()
    return int(entry_id)


def rows(conn: Connection) -> list[DictRow]:
    conn.rollback()
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM todo_brands ORDER BY id").fetchall()


# --- The list -------------------------------------------------------------------


def test_the_biggest_brands_come_first(
    web_app: Flask, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Small", "Q1", estimation=10)
    add(migrated_conn, "Big", "Q2", estimation=500)
    add(migrated_conn, "Unknown", "Q3")

    res = web_app.test_client().get("/todo")

    assert res.status_code == 200
    (context,) = rendered
    assert [e["brand_name"] for e in context["entries"]] == ["Big", "Small", "Unknown"]
    assert context["total"] == 3
    assert context["current_user_id"] is None


def test_the_list_can_be_sorted_on_a_column(
    web_app: Flask, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Bravo", "Q1", estimation=10)
    add(migrated_conn, "Alpha", "Q2", estimation=500)

    web_app.test_client().get("/todo?sort=name&dir=asc")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Alpha", "Bravo"]

    web_app.test_client().get("/todo?sort=not-a-column")
    # An unknown column is the default, not an injection.
    assert rendered[-1]["sort"] == "estimation"


def test_brands_already_in_atp_are_hidden_unless_asked(
    web_app: Flask, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Known", "Q1", estimation=10)
    add(migrated_conn, "Missing", "Q2", estimation=10)
    with migrated_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO atp_places (id, spider_id, brand_wikidata, brand)"
            " VALUES ('atp-1', 'known_fr', 'Q1', 'Known')"
        )
    migrated_conn.commit()

    web_app.test_client().get("/todo")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Missing"]
    assert rendered[-1]["total"] == 2

    web_app.test_client().get("/todo?show_in_atp=1")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Known", "Missing"]


def test_a_brand_known_to_atp_by_its_name_only_is_hidden_too(
    web_app: Flask, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Babylone")  # no wikidata
    with migrated_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO atp_places (id, spider_id, brand_wikidata, brand)"
            " VALUES ('atp-1', 'babylone_fr', 'Q9', 'babylone')"
        )
    migrated_conn.commit()
    web_app.test_client().get("/todo")
    assert rendered[-1]["entries"] == []


def test_the_search_and_the_user_filter_narrow_the_list(
    web_app: Flask, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Babylone", "Q1", user=42)
    add(migrated_conn, "Nouvelle", "Q2", user=43)

    web_app.test_client().get("/todo?q=baby")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Babylone"]

    web_app.test_client().get("/todo?q=Q2")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Nouvelle"]

    web_app.test_client().get("/todo?user=43")
    assert [e["brand_name"] for e in rendered[-1]["entries"]] == ["Nouvelle"]
    assert rendered[-1]["filter_users"] == [(42, "user-42"), (43, "user-43")]


def test_the_signed_in_contributor_is_told_apart(
    contributor: FlaskClient, migrated_conn: Connection, rendered: list[dict[str, Any]]
) -> None:
    add(migrated_conn, "Babylone", "Q1", user=42)
    contributor.get("/todo")
    assert rendered[-1]["current_user_id"] == 42


# --- /todo/check ------------------------------------------------------------------


def test_check_finds_the_brand_by_its_code_first(web_app: Flask, migrated_conn: Connection) -> None:
    add(migrated_conn, "Babylone", "Q1")
    add(migrated_conn, "Babylone Kids", "Q2")
    res = web_app.test_client().get("/todo/check?wikidata=Q2&name=Babylone")
    assert [m["brand_wikidata"] for m in one(res.json)["matches"]] == ["Q2"]


def test_check_falls_back_on_the_name(web_app: Flask, migrated_conn: Connection) -> None:
    add(migrated_conn, "Babylone", "Q1")
    add(migrated_conn, "Babylone Kids", "Q2")
    add(migrated_conn, "Other", "Q3")
    res = web_app.test_client().get("/todo/check?wikidata=Q999&name=babyl")
    assert sorted(m["brand_wikidata"] for m in one(res.json)["matches"]) == ["Q1", "Q2"]


def test_check_with_nothing_to_look_for_finds_nothing(
    web_app: Flask, migrated_conn: Connection
) -> None:
    add(migrated_conn, "Babylone", "Q1")
    assert web_app.test_client().get("/todo/check").json == {"matches": []}


# --- Adding -------------------------------------------------------------------------


def test_a_contributor_adds_a_brand(contributor: FlaskClient, migrated_conn: Connection) -> None:
    res = contributor.post(
        "/todo", json={"brand_wikidata": " Q1 ", "brand_name": " Babylone ", "estimation": "120"}
    )
    assert res.status_code == 201
    (row,) = rows(migrated_conn)
    assert (row["brand_wikidata"], row["brand_name"], row["estimation"], row["osm_user_id"]) == (
        "Q1",
        "Babylone",
        120,
        42,
    )


def test_a_brand_without_a_code_is_accepted(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    assert (
        contributor.post("/todo", json={"brand_name": "Babylone", "brand_wikidata": ""}).status_code
        == 201
    )
    assert rows(migrated_conn)[0]["brand_wikidata"] is None


def test_the_name_is_required(contributor: FlaskClient, migrated_conn: Connection) -> None:
    res = contributor.post("/todo", json={"brand_wikidata": "Q1", "brand_name": "  "})
    assert res.status_code == 400
    assert "error" in one(res.json)
    assert rows(migrated_conn) == []


def test_the_estimation_must_be_a_number(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    res = contributor.post("/todo", json={"brand_name": "Babylone", "estimation": "many"})
    assert res.status_code == 400
    assert rows(migrated_conn) == []


def test_the_same_code_twice_is_a_conflict(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    add(migrated_conn, "Babylone", "Q1")
    res = contributor.post("/todo", json={"brand_wikidata": "Q1", "brand_name": "Babylone bis"})
    assert res.status_code == 409
    assert len(rows(migrated_conn)) == 1


def test_an_anonymous_visitor_cannot_add(web_app: Flask, migrated_conn: Connection) -> None:
    assert web_app.test_client().post("/todo", json={"brand_name": "Babylone"}).status_code == 403
    assert rows(migrated_conn) == []


# --- Updating -----------------------------------------------------------------------


def test_an_entry_is_updated_and_signed(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    entry_id = add(migrated_conn, "Babylone", "Q1", user=43)
    res = contributor.put(
        f"/todo/{entry_id}",
        json={"brand_wikidata": "Q1", "brand_name": "Babylone Paris", "estimation": 50},
    )
    assert res.status_code == 204
    (row,) = rows(migrated_conn)
    assert row["brand_name"] == "Babylone Paris"
    assert row["estimation"] == 50
    assert row["osm_user_id"] == 43  # the author stays
    assert row["updated_by"] == 42
    assert row["updated_at"] is not None


def test_updating_an_unknown_entry_is_not_found(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    assert contributor.put("/todo/999", json={"brand_name": "x"}).status_code == 404


def test_updating_onto_another_entry_code_is_a_conflict(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    add(migrated_conn, "Babylone", "Q1")
    other = add(migrated_conn, "Nouvelle", "Q2")
    res = contributor.put(f"/todo/{other}", json={"brand_wikidata": "Q1", "brand_name": "Nouvelle"})
    assert res.status_code == 409
    assert [r["brand_wikidata"] for r in rows(migrated_conn)] == ["Q1", "Q2"]


def test_an_update_needs_a_name_too(contributor: FlaskClient, migrated_conn: Connection) -> None:
    entry_id = add(migrated_conn, "Babylone", "Q1")
    assert contributor.put(f"/todo/{entry_id}", json={"brand_name": ""}).status_code == 400
    assert rows(migrated_conn)[0]["brand_name"] == "Babylone"


# --- Deleting -----------------------------------------------------------------------


def test_an_author_deletes_their_own_entry(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    entry_id = add(migrated_conn, "Babylone", "Q1", user=42)
    assert contributor.delete(f"/todo/{entry_id}").status_code == 204
    assert rows(migrated_conn) == []


def test_nobody_deletes_someone_else_s_entry(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    entry_id = add(migrated_conn, "Babylone", "Q1", user=43)
    assert contributor.delete(f"/todo/{entry_id}").status_code == 403
    assert len(rows(migrated_conn)) == 1


def test_deleting_an_unknown_entry_is_not_found(
    contributor: FlaskClient, migrated_conn: Connection
) -> None:
    assert contributor.delete("/todo/999").status_code == 404


def test_an_anonymous_visitor_cannot_delete(web_app: Flask, migrated_conn: Connection) -> None:
    entry_id = add(migrated_conn, "Babylone", "Q1")
    assert web_app.test_client().delete(f"/todo/{entry_id}").status_code == 403
