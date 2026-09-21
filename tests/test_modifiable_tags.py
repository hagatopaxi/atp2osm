"""The wave-2 comparison, run as SQL.

MATCHED_POI_SQL decides on its own which tags ATP would replace — the brand
list and /validate both read that decision, so it is worth executing rather
than reading. The tables here are shaped like mv_places and atp_places, with
only the columns the expression touches.
"""

import json
import pathlib
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from src.config import Database
from src.db import code_sql
from src.matching import matched_poi_sql
from src.phone import ensure_normalize_phone
from tests.conftest import Connection

SCHEMA = """
    DROP TABLE IF EXISTS mv_places;
    DROP TABLE IF EXISTS atp_places;
    CREATE TABLE mv_places (
        osm_id BIGINT, node_type TEXT, tags JSONB, name TEXT,
        brand_wikidata TEXT, brand_wikidata_source TEXT, nsi_tags JSONB,
        brand TEXT, city TEXT, postcode TEXT, opening_hours TEXT,
        website TEXT, phone TEXT, email TEXT, version INT,
        osm_timestamp TIMESTAMPTZ, members JSONB, geom geometry(Point, 4326)
    );
    CREATE TABLE atp_places (
        id TEXT, brand TEXT, brand_wikidata TEXT, name TEXT, email TEXT,
        phone TEXT, website TEXT, opening_hours TEXT, country TEXT,
        city TEXT, source_uri TEXT, source_type TEXT, spider_id TEXT,
        postcode TEXT, subdivision_code TEXT, subdivision_name TEXT,
        category TEXT[], geom TEXT
    );
"""

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
OPENING_HOURS_FN = MIGRATIONS / "026_normalize_opening_hours_fn.sql"
# MATCHED_POI_SQL calls osm_primary_tag() to guard the match on the name.
PRIMARY_TAG_FN = MIGRATIONS / "019_create_nsi_brands.sql"
POINT = "ST_SetSRID(ST_MakePoint(2.35, 48.85), 4326)"
GEOJSON = '{"type":"Point","coordinates":[2.35,48.85]}'


def primary_tag_fn() -> str:
    """Just osm_primary_tag() out of the NSI migration: the rest of that file
    builds tables this test has no use for.
    """
    body = PRIMARY_TAG_FN.read_text()
    start = body.index("CREATE OR REPLACE FUNCTION osm_primary_tag")
    return body[start : body.index("$$ LANGUAGE sql", start)] + "$$ LANGUAGE sql IMMUTABLE;"


@pytest.fixture
def places(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as conn:
        ensure_normalize_phone(conn)
        conn.execute(code_sql(OPENING_HOURS_FN.read_text()))
        conn.execute(code_sql(primary_tag_fn()))
        conn.execute(SCHEMA)
        conn.commit()
        yield conn


def modifiable(
    conn: Connection, osm_tags: dict[str, str], **atp: str
) -> tuple[dict[str, str] | None, bool | None]:
    """What wave 2 would replace on an object carrying *osm_tags*."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("TRUNCATE mv_places, atp_places")
        cur.execute(
            f"""INSERT INTO mv_places (osm_id, node_type, tags, name,
                    brand_wikidata, brand, opening_hours, website, phone, email,
                    version, geom)
                VALUES (1, 'node', %s::jsonb, %s, 'Q1', 'Babylone',
                    %s, COALESCE(%s, %s), COALESCE(%s, %s), COALESCE(%s, %s),
                    1, {POINT})""",  # noqa: S608 — a constant of the test
            (
                json.dumps(osm_tags),
                osm_tags.get("name"),
                osm_tags.get("opening_hours"),
                osm_tags.get("website"),
                osm_tags.get("contact:website"),
                osm_tags.get("phone"),
                osm_tags.get("contact:phone"),
                osm_tags.get("email"),
                osm_tags.get("contact:email"),
            ),
        )
        cur.execute(
            """INSERT INTO atp_places (id, brand, brand_wikidata, name, email,
                   phone, website, opening_hours, subdivision_code, geom)
               VALUES ('a1', 'Babylone', 'Q1', 'Babylone', %s, %s, %s, %s, '75', %s)""",
            (
                atp.get("email"),
                atp.get("phone"),
                atp.get("website"),
                atp.get("opening_hours"),
                GEOJSON,
            ),
        )
        row = cur.execute(code_sql(matched_poi_sql())).fetchone()
    return (row or {}).get("modifiable_tags"), (row or {}).get("is_modifiable")


def test_a_differing_value_is_modifiable(places: Connection) -> None:
    tags, flag = modifiable(places, {"phone": "01 23 45 67 89"}, phone="+33 8 20 33 22 11")
    assert flag is True
    assert tags == {"phone": "+33 8 20 33 22 11"}


def test_the_same_number_written_differently_is_not_a_difference(places: Connection) -> None:
    """normalize_phone is what makes the two writings meet."""
    tags, flag = modifiable(places, {"phone": "01 23 45 67 89"}, phone="+33 1 23 45 67 89")
    assert (tags, flag) == ({}, False)


def test_a_contact_writing_is_compared_too(places: Connection) -> None:
    tags, _ = modifiable(
        places, {"contact:website": "https://old.example"}, website="https://babylone.fr"
    )
    assert tags == {"website": "https://babylone.fr"}


def test_a_stale_contact_writing_is_seen_behind_an_up_to_date_one(places: Connection) -> None:
    """mv_places.phone COALESCEs one over the other; the raw tags do not."""
    tags, _ = modifiable(
        places,
        {"phone": "01 23 45 67 89", "contact:phone": "01 00 00 00 00"},
        phone="01 23 45 67 89",
    )
    assert tags == {"phone": "01 23 45 67 89"}


def test_a_missing_tag_is_wave_1s_business(places: Connection) -> None:
    tags, flag = modifiable(places, {"name": "Babylone"}, phone="0123456789")
    assert (tags, flag) == ({}, False)


def test_whitespace_around_separators_is_not_a_difference(places: Connection) -> None:
    """node/12625718605: the same hours, ATP's without the spaces after commas."""
    tags, flag = modifiable(
        places,
        {"opening_hours": "Mo-Th 08:00-12:00, 14:00-18:00; Fr 08:00-12:00, 14:00-17:00"},
        opening_hours="Mo-Th 08:00-12:00,14:00-18:00; Fr 08:00-12:00,14:00-17:00",
    )
    assert (tags, flag) == ({}, False)


def test_a_spelling_is_not_a_difference(places: Connection) -> None:
    """`Su closed`, a rule per day, a split midnight: ATP's dialect, same hours."""
    tags, flag = modifiable(
        places,
        {"opening_hours": "Mo-We,Fr 09:00-02:00; Th 09:00-12:00; PH off"},
        opening_hours="Mo 09:00-24:00; Tu-We 00:00-02:00,09:00-24:00; "
        "Th 00:00-02:00,09:00-12:00; Fr 09:00-24:00; Sa 00:00-02:00; Su closed",
    )
    assert (tags, flag) == ({}, False)


def test_the_replacement_is_written_the_osm_way(places: Connection) -> None:
    tags, _ = modifiable(
        places,
        {"opening_hours": "Mo-Fr 08:00-18:00"},
        opening_hours="Mo-Fr 08:00-19:00; Sa closed",
    )
    assert tags == {"opening_hours": "Mo-Fr 08:00-19:00; Sa off"}


def test_a_closed_day_alone_is_not_worth_a_changeset(places: Connection) -> None:
    tags, flag = modifiable(
        places,
        {"opening_hours": "Mo-Fr 08:00-18:00"},
        opening_hours="Mo-Fr 08:00-18:00; Sa closed",
    )
    assert (tags, flag) == ({}, False)


def test_different_hours_still_are(places: Connection) -> None:
    tags, _ = modifiable(
        places,
        {"opening_hours": "Mo-Fr 08:00-18:00"},
        opening_hours="Mo-Fr 08:00-19:00",
    )
    assert tags == {"opening_hours": "Mo-Fr 08:00-19:00"}


def test_a_week_the_comparison_cannot_read_is_never_proposed(places: Connection) -> None:
    """Seasonal or commented hours: overwriting them is a loss, so they are left to humans."""
    for old in (
        "Jan-Mar Mo-Fr 09:00-12:00; Apr-Dec Mo-Fr 09:00-18:00",
        'Mo-Fr 09:00-12:00 "sur rendez-vous"',
        "PH off",
    ):
        tags, flag = modifiable(places, {"opening_hours": old}, opening_hours="Mo-Fr 08:00-19:00")
        assert (tags, flag) == ({}, False), old
