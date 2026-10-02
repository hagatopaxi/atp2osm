"""The one matching query is built once, with the country's radius in it."""

from src.matching import matched_poi_sql
from src.pipeline.atp2osm import mv_matches_sql, mv_places_brand_sql, mv_places_spider_sql


def test_the_country_radius_lands_in_the_query() -> None:
    assert "500" in matched_poi_sql().as_string()


def test_the_escaped_braces_survive_as_sql() -> None:
    """A second `format` pass would turn '{{}}'::jsonb into a replacement field."""
    assert "'{}'::jsonb" in matched_poi_sql().as_string()


def test_the_counters_and_the_page_read_the_same_matches() -> None:
    """Two diverging copies once made the list show 50 POIs and /validate 60."""
    assert matched_poi_sql().as_string() in mv_matches_sql().as_string()
    assert '"mv_matches"' in mv_places_brand_sql().as_string()
    assert '"mv_matches"' in mv_places_spider_sql().as_string()
