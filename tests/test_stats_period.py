"""Granularity of the statistics charts, derived from the filtered period."""

from datetime import UTC, date, datetime, timedelta

from src.routes.stats import filter_date, period_bounds


def _today() -> date:
    return datetime.now(UTC).date()


def _unit(days: int) -> str:
    stop = _today()
    unit, _start, _end = period_bounds(stop - timedelta(days=days), stop)
    return unit


def test_short_period_is_shown_day_by_day() -> None:
    assert _unit(6) == "day"


def test_medium_period_is_shown_week_by_week() -> None:
    assert _unit(55) == "week"


def test_long_period_is_shown_month_by_month() -> None:
    assert _unit(364) == "month"


def test_without_a_lower_bound_the_series_starts_at_the_first_integration() -> None:
    unit, start, end = period_bounds(None, None)
    assert unit == "month"
    assert "MIN(import_date)" in start.as_string()
    assert _today().isoformat() in end.as_string()


def test_a_filter_value_is_read_as_a_date_or_dropped() -> None:
    assert filter_date("2026-01-02") == date(2026, 1, 2)
    assert filter_date(None) is None
    assert filter_date(7) is None


def test_a_ranking_stacks_the_waves_and_sorts_by_total() -> None:
    from src.routes.stats import stack_by_label

    rows = [
        {"label": "phone", "wave": 1, "value": 3},
        {"label": "website", "wave": 1, "value": 5},
        {"label": "phone", "wave": 2, "value": 4},
    ]
    assert stack_by_label(rows) == [
        {"label": "phone", "value": 7, "by_wave": {1: 3, 2: 4}},
        {"label": "website", "value": 5, "by_wave": {1: 5}},
    ]
