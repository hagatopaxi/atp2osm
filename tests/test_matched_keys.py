"""matched_keys records what the join accepted, on the accepted rows.

The array is the very expression the WHERE filters on — one definition, so
an audit reading it reads the join's own decision. Which key (or keys)
carried a match is then a query away instead of an estimate.
"""

import json
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from src.config import Database
from src.matching import matched_poi_sql
from src.phone import ensure_normalize_phone
from tests.conftest import Connection
from tests.test_modifiable_tags import GEOJSON, POINT, SCHEMA


@pytest.fixture
def places(_migrated: Database) -> Iterator[Connection]:
    # The migrations define the SQL functions the query calls.
    with psycopg.connect(_migrated.conninfo) as conn:
        ensure_normalize_phone(conn)
        conn.execute(SCHEMA)
        conn.commit()
        yield conn


def matched_keys(
    conn: Connection, osm: dict[str, str | None], atp: dict[str, str | None]
) -> list[str] | None:
    """The keys a pair joined on, or None when nothing joined."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("TRUNCATE mv_places, atp_places")
        cur.execute(
            f"""INSERT INTO mv_places (osm_id, node_type, tags, name,
                    brand_wikidata, brand, phone, version, geom)
                VALUES (1, 'node', %s::jsonb, %s, %s, %s, %s, 1, {POINT})""",  # noqa: S608 — a constant of the test
            (
                json.dumps({k: v for k, v in osm.items() if v is not None}),
                osm.get("name"),
                osm.get("brand_wikidata"),
                osm.get("brand"),
                osm.get("phone"),
            ),
        )
        cur.execute(
            """INSERT INTO atp_places (id, brand, brand_wikidata, name, phone,
                   subdivision_code, geom)
               VALUES ('a1', %s, %s, %s, %s, '75', %s)""",
            (
                atp.get("brand"),
                atp.get("brand_wikidata"),
                atp.get("name"),
                atp.get("phone"),
                GEOJSON,
            ),
        )
        row = cur.execute(matched_poi_sql()).fetchone()
    return (row or {}).get("matched_keys")


def test_every_key_that_held_is_recorded_in_declaration_order(
    places: Connection,
) -> None:
    """Brand identity, brand text, name and number all met: all four count."""
    keys = matched_keys(
        places,
        {
            "name": "Babylone",
            "brand": "Babylone",
            "brand_wikidata": "Q1",
            "phone": "01 23 45 67 89",
        },
        {
            "name": "Babylone",
            "brand": "Babylone",
            "brand_wikidata": "Q1",
            "phone": "+33 1 23 45 67 89",
        },
    )
    assert keys == ["brand:wikidata", "brand", "name", "phone"]


def test_a_single_key_is_all_the_row_records(places: Connection) -> None:
    """Nothing but the number is shared: the array says so, and only so."""
    keys = matched_keys(
        places,
        {"phone": "01 23 45 67 89"},
        {"name": "Le Comptoir", "phone": "+33 1 23 45 67 89"},
    )
    assert keys == ["phone"]


def test_a_pair_no_key_reaches_never_becomes_a_row(places: Connection) -> None:
    """The filter and the recording are one expression: no row, no residue."""
    assert (
        matched_keys(
            places,
            {"name": "Babylone"},
            {"name": "Le Comptoir", "phone": "+33 1 23 45 67 89"},
        )
        is None
    )
