"""The object-type filter of the review page.

A batch matched on a name alone holds a fuel station among the bakeries. The
reviewer unticks its type, the POIs carrying it leave the batch, and the choice
is recorded so the next review of the brand replays it — saying so.
"""

import json
from typing import Any

import pytest
from flask.testing import FlaskClient
from psycopg.rows import dict_row

from tests.conftest import Connection, one
from tests.test_review_routes import Rendered, brand, change, give, rendered, stage

__all__ = ["brand", "rendered"]

pytestmark = pytest.mark.usefixtures("guard_on")


def _context(rendered: Rendered) -> dict[str, Any]:
    return next(ctx for name, ctx in rendered if name.endswith("validate.html"))


def _batch(rendered: Rendered) -> list[str]:
    """The OSM ids the page was built on, whatever the sample showed."""
    return [str(i["id"]) for i in _context(rendered)["items"]]


@pytest.fixture
def two_types(brand: Connection, monkeypatch: pytest.MonkeyPatch) -> Connection:
    give(brand, 1, [("75", 2)])
    stage(
        monkeypatch,
        [
            change(1, {"brand": "Babylone"}, {"shop": "bakery"}),
            change(2, {"brand": "Babylone"}, {"amenity": "fuel"}),
        ],
    )
    return brand


def test_every_type_is_offered_with_its_count(
    contributor: FlaskClient, two_types: Connection, rendered: Rendered
) -> None:
    res = contributor.get("/brands/Q1/validate")
    assert res.status_code == 200
    ctx = _context(rendered)
    assert ctx["categories"] == [
        {"tag": "shop=bakery", "count": 1},
        {"tag": "amenity=fuel", "count": 1},
    ]
    assert ctx["excluded"] == frozenset()
    assert not ctx["replayed_filter"]
    assert 'value="shop=bakery"' in res.get_data(as_text=True)


def test_an_unticked_type_leaves_the_batch(
    contributor: FlaskClient, two_types: Connection, rendered: Rendered
) -> None:
    contributor.get("/brands/Q1/validate?filtered=1&keep=shop%3Dbakery")
    ctx = _context(rendered)
    assert _batch(rendered) == ["1"]
    assert ctx["excluded"] == frozenset({"amenity=fuel"})
    # The counts stay those of the unfiltered batch: a dropped type must stay
    # tickable, with the weight it would have had.
    assert ctx["categories"] == [
        {"tag": "shop=bakery", "count": 1},
        {"tag": "amenity=fuel", "count": 1},
    ]


def test_ticking_every_type_keeps_the_whole_batch(
    contributor: FlaskClient, two_types: Connection, rendered: Rendered
) -> None:
    contributor.get("/brands/Q1/validate?filtered=1&keep=shop%3Dbakery&keep=amenity%3Dfuel")
    assert sorted(_batch(rendered)) == ["1", "2"]
    assert _context(rendered)["excluded"] == frozenset()


def test_the_last_choice_is_replayed_and_announced(
    contributor: FlaskClient, two_types: Connection, rendered: Rendered
) -> None:
    with two_types.cursor() as cur:
        cur.execute(
            """INSERT INTO import_history
                   (brand_wikidata, osm_user_id, status, wave, excluded_categories, import_date)
               VALUES ('Q1', 1, 'success', 1, %s, now() - interval '52 weeks')""",
            (["amenity=fuel"],),
        )
    two_types.commit()

    res = contributor.get("/brands/Q1/validate")
    ctx = _context(rendered)
    assert _batch(rendered) == ["1"]
    assert ctx["replayed_filter"]
    assert ctx["excluded"] == frozenset({"amenity=fuel"})
    # The warning is on the page, in whatever language it renders — the
    # unticked box is what says it, the sentence is what makes it deliberate.
    assert "de nouveau décochés" in res.get_data(as_text=True)


def test_unticking_every_type_asks_before_closing_the_brand(
    contributor: FlaskClient, two_types: Connection, rendered: Rendered
) -> None:
    res = contributor.get("/brands/Q1/validate?filtered=1")
    assert res.status_code == 200
    ctx = _context(rendered)
    assert ctx["filtered_out"]
    assert ctx["items"] == []
    assert "confirm_empty=1" in res.get_data(as_text=True)
    with two_types.cursor() as cur:
        assert one(cur.execute("SELECT count(*) FROM import_history").fetchone())[0] == 0


def test_the_confirmation_turns_the_brand_down_and_records_the_filter(
    contributor: FlaskClient, two_types: Connection
) -> None:
    res = contributor.get("/brands/Q1/validate?filtered=1&confirm_empty=1")
    assert res.status_code == 200
    with two_types.cursor(row_factory=dict_row) as cur:
        row = one(cur.execute("SELECT * FROM import_history").fetchone())
    # Not a wave that is done: the reviewer turned every match down.
    assert row["status"] == "cancelled"
    assert row["items_count"] == 0
    # Both lists, always: nothing was integrated, and both types were left out.
    assert row["included_categories"] == []
    assert sorted(row["excluded_categories"]) == ["amenity=fuel", "shop=bakery"]
    assert json.loads(row["comment"]) == [
        {"reasons": ["types_all_excluded"], "comment": "amenity=fuel, shop=bakery"}
    ]
