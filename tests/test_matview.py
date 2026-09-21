from datetime import UTC, datetime

from src.pipeline._matview import signature

SQL = "CREATE MATERIALIZED VIEW mv_x AS SELECT 1"
DAY = datetime(2026, 8, 24, tzinfo=UTC)


def test_same_sql_and_inputs_give_the_same_signature() -> None:
    assert signature(SQL, DAY, None) == signature(SQL, DAY, None)


def test_changing_the_sql_changes_the_signature() -> None:
    assert signature(SQL, DAY) != signature(SQL + " -- edited", DAY)


def test_changing_an_input_changes_the_signature() -> None:
    """The reason this mechanism exists: mv_places reads nsi_brands, so a new

    NSI release must rebuild it even though its SQL and the OSM data are
    unchanged.
    """
    later = datetime(2026, 8, 25, tzinfo=UTC)
    assert signature(SQL, DAY, DAY) != signature(SQL, DAY, later)


def test_inputs_are_positional_not_a_bag() -> None:
    """Two datasources swapping dates is a real change, not a no-op."""
    other = datetime(2026, 1, 1, tzinfo=UTC)
    assert signature(SQL, DAY, other) != signature(SQL, other, DAY)


def test_a_missing_input_differs_from_any_value() -> None:
    assert signature(SQL, None) != signature(SQL, DAY)
