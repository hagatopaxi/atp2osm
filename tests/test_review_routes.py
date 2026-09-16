"""The review of a brand: /brands, then /validate, /confirm, /report-error.

The review page is the one human check before an upload: a tag the reviewer
cannot see is a tag they cannot invalidate. So the page is rendered for real —
the actual templates, on the throwaway database — and the assertions read
both the context handed to the template and the HTML that came out of it.

The matches themselves are staged: `brand_matches` is the spatial join, and
the join has tests of its own. Everything after it runs — the wave, the
cooldowns, the batch, the recent-edit protection, the sample, the page.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest
import requests
from flask import template_rendered
from psycopg.rows import dict_row

import src.osm_history as osm_history
import src.routes.brands as brands
from src.matching import WAVES_BY_NUMBER
from src.osm_history import OsmApiUnavailable

pytestmark = pytest.mark.usefixtures("guard_on")

OLD = datetime.now(timezone.utc) - timedelta(weeks=52)
RECENT = datetime.now(timezone.utc) - timedelta(days=2)


@pytest.fixture
def brand(migrated_conn, monkeypatch):
    """Brand Q1 as the pipeline leaves it: known to ATP, with matches on both
    waves — the second one only counts once the first is done."""
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand")
        cur.execute("""
            CREATE TABLE mv_places_brand (
                brand TEXT, brand_wikidata TEXT, subdivision_code TEXT,
                wave SMALLINT, total BIGINT
            )
        """)
        cur.execute(
            "INSERT INTO atp_places (id, spider_id, brand_wikidata, brand)"
            " VALUES ('atp-1', 'babylone_fr', 'Q1', 'Babylone')"
        )
    migrated_conn.commit()
    # No user-name lookup: the OSM API is not part of this page's contract.
    monkeypatch.setattr(brands, "fetch_osm_users", lambda ids: {})
    yield migrated_conn
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand")
    migrated_conn.commit()


def give(conn, wave, subdivisions):
    """What mv_places_brand announces: (subdivision, count) on that wave."""
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO mv_places_brand VALUES ('Babylone', 'Q1', %s, %s, %s)",
            [(sub, wave, count) for sub, count in subdivisions],
        )
    conn.commit()


def stage(monkeypatch, changes, wave=1):
    """The proposals of the spatial join, on one wave: the other wave has
    none, as when every proposal of a brand is of the same kind."""
    monkeypatch.setattr(
        brands, "brand_matches",
        lambda wikidata, w: list(changes) if w == wave else [],
    )


def api_down(monkeypatch):
    """Every read on the OSM API fails as it does during an outage."""
    def refused(*args, **kwargs):
        raise requests.ConnectionError("connection refused")
    monkeypatch.setattr(osm_history.requests, "get", refused)


def change(id, tag, old_tag, sub="75", name="Paris", edited=OLD, **extra):
    """A proposal as apply_on_node builds it."""
    return {
        "id": id,
        "node_type": "node",
        "version": 3,
        "tag": dict(tag),
        "members": None,
        "lon": 2.35,
        "lat": 48.85,
        "atp_brand": "Babylone",
        "atp_id": f"atp-{id}",
        "spider_id": "babylone_fr",
        "source_uri": "https://babylone.example/stores/1",
        "source_type": None,
        "postcode": "75001",
        "old_tag": dict(old_tag),
        "osm_timestamp": edited.isoformat(),
        "brand_wikidata_source": "osm",
        "subdivision_code": sub,
        "subdivision_name": name,
        **extra,
    }


@pytest.fixture
def rendered(web_app):
    """The context of every template rendered by the next request."""
    contexts = []

    def record(sender, template, context, **extra):
        contexts.append((template.name, context))

    template_rendered.connect(record, web_app)
    yield contexts
    template_rendered.disconnect(record, web_app)


def history(conn):
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute("SELECT * FROM import_history ORDER BY id").fetchall()


# --- Access -------------------------------------------------------------------


def test_an_anonymous_visitor_is_refused(web_app, brand):
    assert web_app.test_client().get("/brands/Q1/validate").status_code == 403


def test_an_expired_session_is_sent_home(web_app, brand):
    with web_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = {"osm_id": 42}  # no token
        res = client.get("/brands/Q1/validate")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/?session_expired=1")


# --- Nothing left ---------------------------------------------------------------


def test_a_brand_with_nothing_left_is_closed_as_integrated(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [])

    res = contributor.get("/brands/Q1/validate")

    assert res.status_code == 200
    assert rendered[0][0] == "brands/:brand_wikidata/empty.html"
    (entry,) = history(brand)
    assert entry["status"] == "success"
    assert entry["items_count"] == 0
    assert entry["osm_user_id"] == 42
    assert entry["brand_name"] == "Babylone"
    assert entry["wave"] == 1


def test_a_brand_unknown_to_atp_is_closed_without_a_name(contributor, brand, monkeypatch):
    with brand.cursor() as cur:
        cur.execute("DELETE FROM atp_places")
    brand.commit()
    stage(monkeypatch, [])

    assert contributor.get("/brands/Q1/validate").status_code == 200
    (entry,) = history(brand)
    assert entry["brand_name"] is None
    # Nothing to give on any wave: closed on the last one.
    assert entry["wave"] == WAVES_BY_NUMBER[max(WAVES_BY_NUMBER)].number


def test_a_brand_whose_matches_are_all_under_cooldown_is_closed(contributor, brand, monkeypatch):
    """The matches exist, the batch is empty: the page has nothing to show.

    Wave 1 blocked whole, the brand is on its last wave, which has nothing —
    and that is the wave the closing row carries."""
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {})])
    with brand.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave)"
            " VALUES ('Q1', 42, 'success', 1, 1) RETURNING id"
        )
        import_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO import_subdivisions (import_id, subdivision_code, subdivision_name, items_count, status)"
            " VALUES (%s, '75', 'Paris', 1, 'success')",
            (import_id,),
        )
    brand.commit()

    assert contributor.get("/brands/Q1/validate").status_code == 200
    assert [(e["items_count"], e["wave"]) for e in history(brand)] == [(1, 1), (0, 2)]


# --- What the reviewer sees -----------------------------------------------------


def _written_and_replaced(context):
    """(new value, old value or None) for every tag the batch would write."""
    seen = []
    for item in context["items"]:
        for key in item["written_tags_keys"]:
            old = item["old_tag"].get(key) if key in item["replaced_tags_keys"] else None
            seen.append((key, item["tag"][key], old))
    return seen


def test_every_written_value_is_on_the_page(contributor, brand, monkeypatch, rendered):
    """Added, replaced, detailed or not: what will be written is shown, and
    what it replaces is shown beside it."""
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [
        change(
            1,
            tag={
                "brand": "Babylone",
                "name": "Babylone Louvre",
                "phone": "+33 1 98 76 54 32",
                "website": "https://babylone.example/louvre",
                "opening_hours": "Mo-Sa 10:00-19:30",
                "shop": "clothes",
                "clothes": "women",
            },
            old_tag={
                "brand": "Babylone",
                "name": "Babylon",
                "opening_hours": "Mo-Sa 10:00-19:00",
            },
        )
    ])

    res = contributor.get("/brands/Q1/validate")

    assert res.status_code == 200
    page = res.text
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    (item,) = context["items"]
    assert set(item["new_tags_keys"]) == {"phone", "website", "shop", "clothes"}
    assert set(item["replaced_tags_keys"]) == {"name", "opening_hours"}
    assert item["other_new_tags"] == {"shop": "clothes", "clothes": "women"}
    # The opening hours are the one value shown in pieces.
    assert set(item["diff"]) == {"opening_hours"}
    for key, new, old in _written_and_replaced(context):
        assert new in page, f"{key}={new} is written but not shown"
        if old is not None:
            assert old in page, f"{key}={old} is replaced but not shown"


def test_an_unchanged_tag_is_not_announced(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"brand": "Babylone", "phone": "+33 1 00 00 00 00"}, {"brand": "Babylone"})
    ])
    contributor.get("/brands/Q1/validate")
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    (item,) = context["items"]
    assert item["written_tags_keys"] == ["phone"]
    assert item["other_new_tags"] == {}
    assert item["diff"] == {}


def test_a_contact_variant_replaced_beside_the_plain_key_is_shown_too(
    contributor, brand, monkeypatch, rendered
):
    """Wave 2 rewrites every writing present: an object carrying both `phone`
    and `contact:phone` gets both replaced, and the reviewer must see both
    old values — they may differ."""
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(
            1,
            tag={"phone": "+33 1 00 00 00 00", "contact:phone": "+33 1 00 00 00 00"},
            old_tag={"phone": "+33 1 11 11 11 11", "contact:phone": "+33 1 22 22 22 22"},
        )
    ], wave=2)

    res = contributor.get("/brands/Q1/validate")

    assert res.status_code == 200
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    for key, new, old in _written_and_replaced(context):
        assert new in res.text, f"{key}={new} is written but not shown"
        assert old in res.text, f"{key}={old} is replaced but not shown"


def test_the_title_falls_back_on_the_brand_when_the_object_has_no_name(
    contributor, brand, monkeypatch, rendered
):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {})])
    contributor.get("/brands/Q1/validate")
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["items"][0]["title"] == "Babylone - 75001"
    assert context["brand"] == "Babylone"


def test_an_inferred_wikidata_code_is_flagged(contributor, brand, monkeypatch):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {}, brand_wikidata_source="nsi")
    ])
    res = contributor.get("/brands/Q1/validate")
    assert "name-suggestion-index" in res.text


# --- The batch and its sample -----------------------------------------------------


def test_the_page_announces_the_batch_and_reviews_a_sample(contributor, brand, monkeypatch, rendered):
    wave = WAVES_BY_NUMBER[1]
    give(brand, 1, [("75", 6)])
    stage(monkeypatch, [
        change(i, {"website": f"https://babylone.example/{i}"}, {}) for i in range(6)
    ])

    contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["size"] == 6
    assert len(context["items"]) == wave.sample_size
    assert context["wave_number"] == 1
    assert context["scope"] == [{"number": "75", "name": "Paris", "count": 6}]


def test_the_sample_covers_every_written_tag(contributor, brand, monkeypatch, rendered):
    """Whatever the draw, a tag written somewhere in the batch is reviewed on
    at least one POI."""
    random.seed(4)
    give(brand, 1, [("75", 12)])
    changes = [change(i, {"website": f"https://babylone.example/{i}"}, {}) for i in range(10)]
    changes.append(change(10, {"phone": "+33 1 00 00 00 00"}, {}))
    changes.append(change(11, {"email": "a@babylone.example"}, {}))
    stage(monkeypatch, changes)

    contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    reviewed = set().union(*(item["written_tags_keys"] for item in context["items"]))
    assert reviewed == {"website", "phone", "email"}


def test_the_scope_lists_the_subdivisions_biggest_first(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 1), ("33", 2)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {}),
        change(2, {"phone": "+33 5 00 00 00 00"}, {}, sub="33", name="Gironde"),
        change(3, {"phone": "+33 5 00 00 00 01"}, {}, sub="33", name="Gironde"),
    ])
    contributor.get("/brands/Q1/validate")
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["scope"] == [
        {"number": "33", "name": "Gironde", "count": 2},
        {"number": "75", "name": "Paris", "count": 1},
    ]


def test_a_subdivision_under_cooldown_is_left_out_of_the_batch(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 1), ("33", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {}),
        change(2, {"phone": "+33 5 00 00 00 00"}, {}, sub="33", name="Gironde"),
    ])
    with brand.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave)"
            " VALUES ('Q1', 42, 'success', 1, 1) RETURNING id"
        )
        import_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO import_subdivisions (import_id, subdivision_code, subdivision_name, items_count, status)"
            " VALUES (%s, '75', 'Paris', 1, 'success')",
            (import_id,),
        )
    brand.commit()

    contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["scope"] == [{"number": "33", "name": "Gironde", "count": 1}]
    assert [item["id"] for item in context["items"]] == [2]


def test_the_batch_never_exceeds_the_wave_size(contributor, brand, monkeypatch, rendered):
    wave = WAVES_BY_NUMBER[1]
    give(brand, 1, [("75", wave.batch_size + 5)])
    stage(monkeypatch, [
        change(i, {"website": f"https://babylone.example/{i}"}, {})
        for i in range(wave.batch_size + 5)
    ])
    contributor.get("/brands/Q1/validate")
    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["size"] == wave.batch_size


def test_the_last_import_is_recalled(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {})])
    with brand.cursor() as cur:
        cur.execute(
            "INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, items_count, wave, import_date)"
            " VALUES ('Q1', 43, 'error', 'OSM API timeout', 0, 1, NOW() - INTERVAL '2 months')"
        )
    brand.commit()

    res = contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["last_import"]["status"] == "error"
    assert "OSM API timeout" in res.text


# --- Wave 2 -------------------------------------------------------------------------


def test_wave_2_reviews_the_whole_batch(contributor, brand, monkeypatch, rendered):
    wave = WAVES_BY_NUMBER[2]
    give(brand, 2, [("75", wave.batch_size + 2)])
    stage(monkeypatch, [
        change(i, {"phone": "+33 1 00 00 00 00"}, {"phone": f"+33 1 11 11 11 {i:02d}"})
        for i in range(wave.batch_size + 2)
    ], wave=2)

    contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert context["wave_number"] == 2
    assert context["size"] == wave.batch_size
    assert len(context["items"]) == wave.batch_size


def test_wave_2_leaves_a_recent_human_value_alone(contributor, brand, monkeypatch, rendered):
    """The protection runs on the page too: what it keeps, the reviewer never
    sees proposed."""
    give(brand, 2, [("75", 2)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}),
        change(2, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 22 22 22 22"}, edited=RECENT),
    ], wave=2)
    monkeypatch.setattr(
        osm_history, "versions",
        lambda node_type, osm_id: [
            {"version": 1, "timestamp": RECENT.isoformat(), "changeset": 9,
             "tags": {"phone": "+33 1 22 22 22 22"}}
        ],
    )
    monkeypatch.setattr(osm_history, "is_bot", lambda changeset: False)

    contributor.get("/brands/Q1/validate")

    _, context = next(c for c in rendered if c[0].endswith("validate.html"))
    assert [item["id"] for item in context["items"]] == [1]
    assert context["size"] == 1


def test_wave_2_cannot_decide_when_the_api_is_down(contributor, brand, monkeypatch, rendered):
    """Not a brand with nothing left: nothing is recorded, the page says so."""
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}, edited=RECENT),
    ], wave=2)
    api_down(monkeypatch)

    res = contributor.get("/brands/Q1/validate")

    assert res.status_code == 503
    assert rendered[0][0] == "errors/503.html"
    assert history(brand) == []


def test_wave_1_needs_no_api(contributor, brand, monkeypatch):
    """Adding a tag overwrites nothing: an OSM outage does not touch wave 1."""
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {}, edited=RECENT)])
    assert contributor.get("/brands/Q1/validate").status_code == 200


def test_an_upload_cannot_go_through_while_the_api_is_down(contributor, brand, monkeypatch):
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}, edited=RECENT),
    ], wave=2)
    api_down(monkeypatch)
    monkeypatch.setattr(brands, "BulkUpload", _never_called)

    res = contributor.post("/brands/Q1/upload")

    assert res.status_code == 503
    assert res.json == {"errors": ["OSM API unavailable"]}
    assert history(brand) == []


def test_a_rejection_cannot_be_filed_while_the_api_is_down(contributor, brand, monkeypatch):
    """report-error reads the batch for its wave: the outage stops it too,
    rather than filing a cancellation on a wave nobody could review."""
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}, edited=RECENT),
    ], wave=2)
    api_down(monkeypatch)
    res = contributor.post("/brands/Q1/report-error", json={"comment": "x", "brand_name": "Babylone"})
    assert res.status_code == 503
    assert history(brand) == []


def _never_called(*args, **kwargs):
    raise AssertionError("the route must not reach OSM here")


# --- report-error ---------------------------------------------------------------------


def test_a_rejection_is_filed_as_cancelled_on_the_brand_wave(contributor, brand, monkeypatch):
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}),
    ], wave=2)

    res = contributor.post(
        "/brands/Q1/report-error",
        json={"comment": '[{"reasons": ["wrong_brand"]}]', "brand_name": "Babylone"},
    )

    assert res.status_code == 201
    (entry,) = history(brand)
    assert entry["id"] == res.json["id"]
    assert entry["status"] == "cancelled"
    assert entry["wave"] == 2
    assert entry["brand_name"] == "Babylone"
    assert entry["osm_user_id"] == 42
    assert '"wrong_brand"' in entry["comment"]


def test_a_rejection_without_a_body_is_a_bad_request(contributor, brand, monkeypatch):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {})])
    res = contributor.post("/brands/Q1/report-error", data="not json",
                           content_type="text/plain")
    assert res.status_code in (400, 415)
    assert history(brand) == []


# --- /brands: the list ---------------------------------------------------------------


def integrate(conn, sub, wave, status="success", when="NOW()"):
    """An integration of one subdivision: `status` is the changeset's, the
    import's follows from it."""
    with conn.cursor() as cur:
        cur.execute(
            f"""INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave, import_date)
                VALUES ('Q1', 42, %s, 1, %s, {when}) RETURNING id""",
            ("success" if status == "success" else "error", wave),
        )
        import_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO import_subdivisions (import_id, subdivision_code, subdivision_name, items_count, status)"
            " VALUES (%s, %s, %s, 1, %s)",
            (import_id, sub, sub, status),
        )
    conn.commit()


def listed(rendered):
    """The context of the latest rendering of the list."""
    return [c for t, c in rendered if t == "brands.html"][-1]


def test_the_list_counts_what_is_left_on_the_current_wave(web_app, brand, rendered):
    give(brand, 1, [("75", 4), ("33", 2)])
    give(brand, 2, [("75", 3)])

    res = web_app.test_client().get("/brands")

    assert res.status_code == 200
    (row,) = listed(rendered)["rows"]
    assert (row["brand_wikidata"], row["wave"], row["total"]) == ("Q1", 1, 6)
    assert row["last_status"] is None
    assert listed(rendered)["wave_counts"] == {1: 1}


def test_an_integrated_subdivision_leaves_the_count(web_app, brand, rendered):
    give(brand, 1, [("75", 4), ("33", 2)])
    integrate(brand, "75", wave=1)

    web_app.test_client().get("/brands")

    (row,) = listed(rendered)["rows"]
    assert (row["wave"], row["total"], row["last_status"]) == (1, 2, "success")


def test_a_brand_done_with_wave_1_moves_to_wave_2(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    give(brand, 2, [("75", 3)])
    integrate(brand, "75", wave=1)

    web_app.test_client().get("/brands")

    (row,) = listed(rendered)["rows"]
    assert (row["wave"], row["total"]) == (2, 3)
    assert listed(rendered)["wave_counts"] == {2: 1}


def test_a_brand_with_nothing_left_is_not_listed(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    integrate(brand, "75", wave=1)
    web_app.test_client().get("/brands")
    assert listed(rendered)["rows"] == []
    assert listed(rendered)["total_brands"] == 0


def test_a_failed_integration_hides_the_subdivision_for_a_shorter_while(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    integrate(brand, "75", wave=1, status="error_osm_api", when="NOW() - INTERVAL '5 weeks'")
    web_app.test_client().get("/brands")
    (row,) = listed(rendered)["rows"]
    assert row["total"] == 4

    integrate(brand, "75", wave=1, status="error_osm_api", when="NOW() - INTERVAL '3 weeks'")
    web_app.test_client().get("/brands")
    assert listed(rendered)["rows"] == []


def test_a_rejected_brand_comes_back_when_a_spider_is_edited(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    with brand.cursor() as cur:
        cur.execute("INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave)"
                    " VALUES ('Q1', 42, 'cancelled', 0, 1)")
        cur.execute("INSERT INTO atp_spiders (spider, updated_at) VALUES ('babylone_fr', NOW() - INTERVAL '1 day')")
    brand.commit()
    web_app.test_client().get("/brands")
    assert listed(rendered)["rows"] == []

    with brand.cursor() as cur:
        cur.execute("UPDATE atp_spiders SET updated_at = NOW() + INTERVAL '1 hour'")
    brand.commit()
    web_app.test_client().get("/brands")
    (row,) = listed(rendered)["rows"]
    assert row["last_status"] == "cancelled"


def test_the_filters_narrow_the_rows_but_not_the_counts(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    with brand.cursor() as cur:
        cur.execute("INSERT INTO mv_places_brand VALUES ('Nouvelle', 'Q2', '75', 2, 1)")
    brand.commit()

    web_app.test_client().get("/brands?q=nouv")

    context = listed(rendered)
    assert [r["brand"] for r in context["rows"]] == ["Nouvelle"]
    assert (context["shown"], context["total_brands"]) == (1, 2)
    assert context["wave_counts"] == {1: 1, 2: 1}
    assert context["filters"] == {"q": "nouv"}


def test_the_list_is_sorted_biggest_first_then_on_request(web_app, brand, rendered):
    give(brand, 1, [("75", 4)])
    with brand.cursor() as cur:
        cur.execute("INSERT INTO mv_places_brand VALUES ('Zed', 'Q2', '75', 1, 9)")
    brand.commit()

    web_app.test_client().get("/brands")
    assert [r["brand"] for r in listed(rendered)["rows"]] == ["Zed", "Babylone"]

    web_app.test_client().get("/brands?sort=brand&dir=asc")
    assert [r["brand"] for r in listed(rendered)["rows"]] == ["Babylone", "Zed"]

    web_app.test_client().get("/brands?sort=nonsense")
    assert [r["brand"] for r in listed(rendered)["rows"]] == ["Zed", "Babylone"]
    assert listed(rendered)["sort"] == "nonsense"


def test_the_review_is_offered_to_contributors_only(web_app, contributor, brand):
    give(brand, 1, [("75", 4)])
    assert "/brands/Q1/validate" not in web_app.test_client().get("/brands").text
    assert "/brands/Q1/validate" in contributor.get("/brands").text


# --- /confirm -------------------------------------------------------------------------


def test_the_confirmation_sums_up_the_batch(contributor, brand, monkeypatch, rendered):
    give(brand, 1, [("75", 2), ("33", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00", "website": "https://babylone.example"}, {}),
        change(2, {"phone": "+33 1 00 00 00 01"}, {}),
        change(3, {"website": "https://babylone.example/33"}, {}, sub="33", name="Gironde"),
    ])

    res = contributor.get("/brands/Q1/confirm")

    assert res.status_code == 200
    _, context = next(c for c in rendered if c[0].endswith("confirm.html"))
    assert context["stats"]["size"] == 3
    assert context["stats"]["by_tag"] == {"phone": 2, "website": 2}
    assert context["stats"]["total_tag_updates"] == 4
    assert context["stats"]["by_subdivision"] == {
        "33": {"name": "Gironde", "count": 1},
        "75": {"name": "Paris", "count": 2},
    }
    assert context["wave_number"] == 1
    # The log the page offers is the batch itself.
    assert '"id": 3' in context["logs"]


def test_an_empty_batch_goes_back_to_the_review(contributor, brand, monkeypatch):
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [])
    res = contributor.get("/brands/Q1/confirm")
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/brands/Q1/validate")
    assert history(brand) == []


def test_a_brand_under_cooldown_cannot_be_confirmed(contributor, brand, monkeypatch):
    """Not in the list: only a forged URL lands here."""
    give(brand, 1, [("75", 1)])
    stage(monkeypatch, [change(1, {"phone": "+33 1 00 00 00 00"}, {})])
    with brand.cursor() as cur:
        cur.execute("INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave)"
                    " VALUES ('Q1', 42, 'cancelled', 0, 1)")
    brand.commit()
    assert contributor.get("/brands/Q1/confirm").status_code == 403


def test_the_confirmation_cannot_decide_when_the_api_is_down(contributor, brand, monkeypatch):
    give(brand, 2, [("75", 1)])
    stage(monkeypatch, [
        change(1, {"phone": "+33 1 00 00 00 00"}, {"phone": "+33 1 11 11 11 11"}, edited=RECENT),
    ], wave=2)
    api_down(monkeypatch)
    assert contributor.get("/brands/Q1/confirm").status_code == 503


def test_an_anonymous_visitor_cannot_confirm(web_app, brand):
    assert web_app.test_client().get("/brands/Q1/confirm").status_code == 403
