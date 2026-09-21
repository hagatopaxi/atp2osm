"""Waves: which typology a brand is on, and what wave 2 writes.

A cooldown belongs to the wave that earned it — that is the whole point of
the `wave` column, and the one thing worth a database test here.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from psycopg.rows import dict_row

from src.matching import (
    WAVES,
    WAVES_BY_NUMBER,
    Change,
    apply_on_node,
    current_wave,
    get_blocked_subdivisions,
)
from tests.conftest import Connection, one


def match(tags: dict[str, str], modifiable: dict[str, str], **extra: Any) -> Change | None:  # noqa: ANN401 — a row's columns
    base: dict[str, Any] = {
        "osm_id": 1,
        "version": 1,
        "node_type": "node",
        "tags": tags,
        "lon": 1,
        "lat": 2,
        "brand": "Babylone",
        "id": "atp-1",
        "source_uri": "https://babylone.fr",
        "source_type": "spider",
        "postcode": "75001",
        "subdivision_code": "75",
        "subdivision_name": "Paris",
        "atp_opening_hours": None,
        "atp_website": None,
        "atp_phone": None,
        "atp_email": None,
        "modifiable_tags": modifiable,
    }
    base.update(extra)
    return apply_on_node(base, wave=2)


def test_wave_2_replaces_the_value_the_object_carries() -> None:
    res = one(match({"website": "https://babylone.fr"}, {"website": "https://www.babylone.fr"}))
    assert res["tag"] == {"website": "https://www.babylone.fr"}


def test_wave_2_writes_on_the_key_that_holds_the_value() -> None:
    """An object tagged contact:phone keeps its own spelling, it grows no second one."""
    res = one(match({"contact:phone": "01 23 45 67 89"}, {"phone": "+33 8 20 33 22 11"}))
    assert res["tag"] == {"contact:phone": "08 20 33 22 11"}


def test_wave_2_rewrites_every_writing_present() -> None:
    """An object must not come out holding two contradictory numbers."""
    res = one(
        match(
            {"phone": "01 23 45 67 89", "contact:phone": "01 00 00 00 00"},
            {"phone": "+33 8 20 33 22 11"},
        )
    )
    assert res["tag"] == {
        "phone": "08 20 33 22 11",
        "contact:phone": "08 20 33 22 11",
    }


def test_wave_2_keeps_what_atp_never_scraped_of_the_hours() -> None:
    """`PH off` was a contributor's; ATP only knows the week."""
    res = one(
        match(
            {"opening_hours": "Mo-Fr 08:00-18:00; Su off; PH off"},
            {"opening_hours": "Mo-Sa 08:00-19:00"},
        )
    )
    assert res["tag"] == {"opening_hours": "Mo-Sa 08:00-19:00; PH off"}


def test_wave_2_never_adds_a_tag() -> None:
    """A hole is wave 1's business, and a brand is only ever on one wave."""
    assert match({"name": "Babylone"}, {"phone": "0123456789"}) is None


def test_every_wave_has_its_own_batch_size() -> None:
    assert [w.number for w in WAVES] == sorted(w.number for w in WAVES)
    assert WAVES_BY_NUMBER[2].batch_size < WAVES_BY_NUMBER[1].batch_size


def test_an_alpha_wave_is_reviewed_in_full() -> None:
    """A wave still in alpha shows a contributor every value it writes."""
    for wave in WAVES:
        if wave.alpha:
            assert wave.sample_size >= wave.batch_size, wave


@pytest.fixture
def brand_waves(migrated_conn: Connection) -> Iterator[Connection]:
    """mv_places_brand as the pipeline builds it, with two waves to give."""
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand")
        cur.execute("""
            CREATE TABLE mv_places_brand (
                brand TEXT, brand_wikidata TEXT, subdivision_code TEXT,
                wave SMALLINT, total BIGINT
            )
        """)
        cur.executemany(
            "INSERT INTO mv_places_brand VALUES ('Babylone', 'Q1', %s, %s, %s)",
            [("75", 1, 4), ("33", 1, 2), ("75", 2, 3)],
        )
    migrated_conn.commit()
    yield migrated_conn
    with migrated_conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS mv_places_brand")
    migrated_conn.commit()


def integrate(conn: Connection, subdivision: str, wave: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, wave)
               VALUES ('Q1', 42, 'success', 1, %s) RETURNING id""",
            (wave,),
        )
        import_id = one(cur.fetchone())[0]
        cur.execute(
            """INSERT INTO import_subdivisions
                   (import_id, subdivision_code, subdivision_name, items_count, status)
               VALUES (%s, %s, %s, 1, 'success')""",
            (import_id, subdivision, subdivision),
        )
    conn.commit()


def test_a_brand_starts_on_its_first_wave(brand_waves: Connection) -> None:
    with brand_waves.cursor(row_factory=dict_row) as cur:
        assert current_wave(cur, "Q1").number == 1


def test_a_wave_1_cooldown_does_not_block_wave_2(brand_waves: Connection) -> None:
    integrate(brand_waves, "75", wave=1)
    with brand_waves.cursor(row_factory=dict_row) as cur:
        assert get_blocked_subdivisions(cur, "Q1", 1) == {"75"}
        assert get_blocked_subdivisions(cur, "Q1", 2) == set()


def test_the_next_wave_comes_only_once_the_first_is_done(brand_waves: Connection) -> None:
    integrate(brand_waves, "75", wave=1)
    with brand_waves.cursor(row_factory=dict_row) as cur:
        assert current_wave(cur, "Q1").number == 1  # 33 is still to do
    integrate(brand_waves, "33", wave=1)
    with brand_waves.cursor(row_factory=dict_row) as cur:
        assert current_wave(cur, "Q1").number == 2


def test_highlight_marks_the_time_not_the_space() -> None:
    from src.routes.brands import highlight_diff

    old, new = highlight_diff(
        "Mo-Th 11:30-14:30, 18:30-22:30; Su 11:30-14:30",
        "Mo-Th 11:30-14:30,18:30-21:30; Su 11:30-14:30",
    )
    assert [t for t, changed in old if changed] == [" 18:30-22:30"]
    assert [t for t, changed in new if changed] == ["18:30-21:30"]
    assert "".join(t for t, _ in new) == "Mo-Th 11:30-14:30,18:30-21:30; Su 11:30-14:30"


def test_highlight_follows_a_split_rule() -> None:
    from src.routes.brands import highlight_diff

    old, new = highlight_diff("Fr-Sa 11:30-14:30", "Fr 11:30-14:30; Sa 11:30-15:00")
    assert all(changed for _, changed in old)
    assert all(changed for _, changed in new)
