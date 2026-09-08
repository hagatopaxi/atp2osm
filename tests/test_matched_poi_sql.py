"""The one matching query is built once, with the country's radius in it."""

from src.matching import matched_poi_sql
from src.pipeline.atp2osm import _mv_places_brand_sql


def test_the_country_radius_lands_in_the_query():
    assert "500" in matched_poi_sql()


def test_the_escaped_braces_survive_as_sql():
    """A second `format` pass would turn '{{}}'::jsonb into a replacement field."""
    query = matched_poi_sql("atp.brand_wikidata = %s")

    assert "'{}'::jsonb" in query
    assert "{where_options}" not in query
    assert "atp.brand_wikidata = %s" in query


def test_the_view_and_the_page_share_the_same_built_query():
    """Two diverging copies once made the list show 50 POIs and /validate 60."""
    assert matched_poi_sql() in _mv_places_brand_sql()
