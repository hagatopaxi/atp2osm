"""The export API must expose only what atp2osm produces, and nothing else."""

import pathlib

import pytest
from flask import Flask

from src.routes.export import DATASETS, csv_value
from tests.conftest import Connection, one

# Tables holding ATP or OSM data (or derived from them) — never exportable.
UPSTREAM_TABLES = {"atp_places", "points", "polygons", "mv_places", "mv_places_brand"}


def test_only_own_tables_are_exposed() -> None:
    tables = {table for _, table, _, _ in DATASETS.values()}
    assert tables.isdisjoint(UPSTREAM_TABLES)
    assert tables == {"import_history", "import_subdivisions", "todo_brands"}


@pytest.mark.parametrize("dataset", DATASETS)
def test_columns_are_distinct_identifiers(dataset: str) -> None:
    names, _, _, _ = DATASETS[dataset]
    assert len(set(names)) == len(names)  # no duplicate column
    assert all(name.isidentifier() for name in names)


def testcsv_value_flattens_json_columns() -> None:
    assert csv_value({"phone": 2}) == '{"phone": 2}'
    assert csv_value([1, 2]) == "[1, 2]"
    assert csv_value("Zara") == "Zara"
    assert csv_value(None) is None


def test_export_routes_stay_out_of_the_sitemap() -> None:
    """The sitemap is built from PUBLIC_PAGES: the API has no place in it."""
    from src.routes.misc import PUBLIC_PAGES

    assert not any(endpoint.startswith("export.") for endpoint in PUBLIC_PAGES)


def test_robots_disallows_the_api() -> None:
    robots = pathlib.Path("website/templates/robots.txt").read_text()
    assert "Disallow: /api/" in robots


# --- The route, on the real schema ---------------------------------------------------


def seed(conn: Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, brand_name, osm_user_id, status, items_count, tags_count)"
            " VALUES ('Q1', 'Babylone', 42, 'success', 3, '{\"phone\": 3}')"
            " RETURNING id"
        )
        import_id = one(cur.fetchone())[0]
        cur.execute(
            "INSERT INTO import_subdivisions (import_id, subdivision_code, subdivision_name, items_count, status)"
            " VALUES (%s, '75', 'Paris', 3, 'success')",
            (import_id,),
        )
        cur.execute(
            "INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id) VALUES ('Q2', 'Missing', 42)"
        )
        cur.execute(
            "INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id) VALUES ('Q3', 'Known', 42)"
        )
        cur.execute(
            "INSERT INTO atp_places (id, spider_id, brand_wikidata, brand) VALUES ('a', 's', 'Q3', 'Known')"
        )
    conn.commit()


def test_json_export_carries_the_rows(web_app: Flask, migrated_conn: Connection) -> None:
    seed(migrated_conn)
    res = web_app.test_client().get("/api/export/history.json")
    assert res.status_code == 200
    assert res.mimetype == "application/json"
    (row,) = one(res.json)
    assert (row["brand_wikidata"], row["status"], row["tags_count"]) == (
        "Q1",
        "success",
        {"phone": 3},
    )


def test_csv_export_has_a_header_and_flat_values(web_app: Flask, migrated_conn: Connection) -> None:
    seed(migrated_conn)
    res = web_app.test_client().get("/api/export/history.csv")
    assert res.mimetype == "text/csv"
    header, row = res.text.strip().splitlines()
    assert header.split(",")[:2] == ["id", "brand_wikidata"]
    assert '"{""phone"": 3}"' in row


def test_subdivisions_export_details_the_changesets(
    web_app: Flask, migrated_conn: Connection
) -> None:
    seed(migrated_conn)
    (row,) = one(web_app.test_client().get("/api/export/subdivisions.json").json)
    assert (row["subdivision_code"], row["items_count"]) == ("75", 3)


def test_the_todo_export_hides_what_atp_knows_like_the_page(
    web_app: Flask, migrated_conn: Connection
) -> None:
    seed(migrated_conn)
    client = web_app.test_client()
    assert [r["brand_wikidata"] for r in one(client.get("/api/export/todo.json").json)] == ["Q2"]
    assert sorted(
        r["brand_wikidata"] for r in one(client.get("/api/export/todo.json?show_in_atp=1").json)
    ) == ["Q2", "Q3"]


def test_the_filters_of_the_page_apply_to_its_export(
    web_app: Flask, migrated_conn: Connection
) -> None:
    seed(migrated_conn)
    client = web_app.test_client()
    assert client.get("/api/export/history.json?status=cancelled").json == []
    assert len(one(client.get("/api/export/history.json?q=baby").json)) == 1


@pytest.mark.parametrize(
    "path", ["/api/export/points.json", "/api/export/history.xml", "/api/export/atp_places.csv"]
)
def test_anything_else_is_not_found(web_app: Flask, migrated_conn: Connection, path: str) -> None:
    assert web_app.test_client().get(path).status_code == 404


def test_the_old_departements_url_still_answers(web_app: Flask, migrated_conn: Connection) -> None:
    res = web_app.test_client().get("/api/export/departements.csv")
    assert res.status_code == 301
    assert res.headers["Location"].endswith("/api/export/subdivisions.csv")
