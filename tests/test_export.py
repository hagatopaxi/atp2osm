"""The export API must expose only what atp2osm produces, and nothing else."""

import pathlib

import pytest

from src.routes.export import DATASETS, _csv_value

# Tables holding ATP or OSM data (or derived from them) — never exportable.
UPSTREAM_TABLES = {"atp_places", "points", "polygons", "mv_places", "mv_places_brand"}


def test_only_own_tables_are_exposed():
    tables = {table for _, table, _, _ in DATASETS.values()}
    assert tables.isdisjoint(UPSTREAM_TABLES)
    assert tables == {"import_history", "import_subdivisions", "todo_brands"}


@pytest.mark.parametrize("dataset", DATASETS)
def test_columns_parse_as_a_csv_header(dataset):
    columns, _, _, _ = DATASETS[dataset]
    names = [c.strip() for c in columns.split(",")]
    assert names == sorted(set(names), key=names.index)  # no duplicate column
    assert all(name.isidentifier() for name in names)


def test_csv_value_flattens_json_columns():
    assert _csv_value({"phone": 2}) == '{"phone": 2}'
    assert _csv_value([1, 2]) == "[1, 2]"
    assert _csv_value("Zara") == "Zara"
    assert _csv_value(None) is None


def test_export_routes_stay_out_of_the_sitemap():
    """The sitemap is built from PUBLIC_PAGES: the API has no place in it."""
    from src.routes.misc import PUBLIC_PAGES

    assert not any(endpoint.startswith("export.") for endpoint in PUBLIC_PAGES)


def test_robots_disallows_the_api():
    robots = pathlib.Path("website/templates/robots.txt").read_text()
    assert "Disallow: /api/" in robots
