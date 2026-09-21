from datetime import UTC, datetime
from typing import Any

from psycopg import sql
from werkzeug.datastructures import MultiDict

from src.routes.history import FILTERS as HISTORY_FILTERS
from src.utils import build_filters, filter_brands, where_clause


def _sql(conditions: list[sql.Composable]) -> str:
    return where_clause(conditions).as_string()


def test_no_filters() -> None:
    assert build_filters(MultiDict(), HISTORY_FILTERS) == ([], [], {})


def test_all_filters() -> None:
    where, params, active = build_filters(
        MultiDict(
            {
                "q": "carre",
                "status": "partial",
                "wave": "2",
                "user": "42",
                "from": "2026-01-01",
                "to": "2026-02-01",
            }
        ),
        HISTORY_FILTERS,
    )
    assert _sql(where).startswith("WHERE ")
    assert _sql(where).count(" AND ") == 5
    assert params == [
        "%carre%",
        "%carre%",
        "partial",
        2,
        42,
        "2026-01-01",
        "2026-02-01",
    ]
    assert active["status"] == "partial"
    assert active["wave"] == 2
    assert active["user"] == 42


def test_unknown_status_and_blanks_ignored() -> None:
    assert build_filters(MultiDict({"status": "bogus", "q": "  ", "to": ""}), HISTORY_FILTERS) == (
        [],
        [],
        {},
    )


def _brand(
    name: str,
    wikidata: str,
    total: int,
    status: str | None = None,
    last_import: datetime | None = None,
) -> dict[str, Any]:
    return {
        "brand": name,
        "brand_wikidata": wikidata,
        "total": total,
        "last_status": status,
        "last_import": last_import,
    }


BRANDS = [
    _brand("Carrefour", "Q217599", 10),
    _brand("Lidl", "Q151954", 500, "success", datetime(2026, 3, 1, tzinfo=UTC)),
    _brand("Aldi", "Q125054", 20, "error", datetime(2026, 1, 15, tzinfo=UTC)),
]


def test_brands_no_filter_keeps_every_brand_whatever_its_size() -> None:
    rows, active = filter_brands(BRANDS, MultiDict())
    assert len(rows) == 3
    assert active == {}


def test_brands_search_status_and_dates() -> None:
    rows, _ = filter_brands(BRANDS, MultiDict({"q": "ald"}))
    assert [r["brand"] for r in rows] == ["Aldi"]

    rows, _ = filter_brands(BRANDS, MultiDict({"status": "error"}))
    assert [r["brand"] for r in rows] == ["Aldi"]

    # never integrated
    rows, _ = filter_brands(BRANDS, MultiDict({"status": "none"}))
    assert [r["brand"] for r in rows] == ["Carrefour"]

    rows, _ = filter_brands(BRANDS, MultiDict({"from": "2026-02-01"}))
    assert [r["brand"] for r in rows] == ["Lidl"]


# Missing-brands page config (importing src.routes.todo would pull the app config).
TODO_FILTERS = {"q": ("brand_name", "brand_wikidata"), "user": "osm_user_id", "date": "created_at"}


def test_filter_absent_from_spec_is_ignored() -> None:
    """The missing-brands page exposes no status: the parameter has no effect."""
    where, params, active = build_filters(
        MultiDict({"status": "success", "user": "7"}), TODO_FILTERS
    )
    assert _sql(where) == 'WHERE "osm_user_id" = %s'
    assert params == [7]
    assert active == {"user": 7}


def test_spec_drives_the_columns() -> None:
    where, _, _ = build_filters(MultiDict({"from": "2026-01-01"}), TODO_FILTERS)
    assert _sql(where) == 'WHERE "created_at" >= %s'
