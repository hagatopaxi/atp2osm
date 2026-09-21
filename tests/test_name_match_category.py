"""A name alone only matches an object of the same kind.

`LOWER(osm.name) = LOWER(atp.name)` used to match anything named after the
place: the information board in front of a creche, the residential landuse
around it, the bicycle station that took its name. The category ATP carries is
what tells them apart, and it is executed here rather than read.
"""

import json
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.rows import dict_row

from src.config import Database
from src.db import code_sql
from src.matching import matched_poi_sql
from src.phone import ensure_normalize_phone
from tests.conftest import Connection
from tests.test_modifiable_tags import (
    GEOJSON,
    OPENING_HOURS_FN,
    POINT,
    SCHEMA,
    primary_tag_fn,
)


@pytest.fixture
def places(test_db: Database) -> Iterator[Connection]:
    with psycopg.connect(test_db.conninfo) as conn:
        ensure_normalize_phone(conn)
        conn.execute(code_sql(OPENING_HOURS_FN.read_text()))
        conn.execute(code_sql(primary_tag_fn()))
        conn.execute(SCHEMA)
        conn.commit()
        yield conn


def matches(conn: Connection, osm_tags: dict[str, str], atp_category: list[str] | None) -> bool:
    """Does an ATP POI of *atp_category* match an OSM object named like it?

    Nothing but the name can join them: the ATP row carries no brand, no
    wikidata, no contact detail.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("TRUNCATE mv_places, atp_places")
        cur.execute(
            f"""INSERT INTO mv_places (osm_id, node_type, tags, name, version, geom)
                VALUES (1, 'node', %s::jsonb, %s, 1, {POINT})""",  # noqa: S608 — a constant of the test
            (json.dumps(osm_tags), osm_tags.get("name")),
        )
        cur.execute(
            """INSERT INTO atp_places (id, name, website, subdivision_code,
                   category, geom)
               VALUES ('a1', 'Les Petits Chaperons Rouges', 'https://lpcr.fr',
                   '75', %s, %s)""",
            (atp_category, GEOJSON),
        )
        return cur.execute(code_sql(matched_poi_sql())).fetchone() is not None


NAMED = {"name": "Les Petits Chaperons Rouges"}
CRECHE = ["amenity", "kindergarten"]


def test_the_same_category_still_matches(places: Connection) -> None:
    assert matches(places, NAMED | {"amenity": "kindergarten"}, CRECHE)


def test_an_information_board_does_not(places: Connection) -> None:
    assert not matches(places, NAMED | {"tourism": "information"}, CRECHE)


def test_a_residential_landuse_does_not(places: Connection) -> None:
    assert not matches(places, NAMED | {"landuse": "residential"}, CRECHE)


def test_a_bicycle_station_does_not(places: Connection) -> None:
    assert not matches(places, NAMED | {"amenity": "bicycle_rental"}, CRECHE)


def test_an_object_with_no_category_of_its_own_does_not(places: Connection) -> None:
    """A bare named node is exactly the noise this guard is against."""
    assert not matches(places, NAMED, CRECHE)


def test_a_poi_atp_has_no_category_for_matches_on_the_name_alone(places: Connection) -> None:
    """4% of the POIs carry none, and the column is NULL until the next ATP
    import: the guard must not hide them.
    """
    assert matches(places, NAMED | {"amenity": "kindergarten"}, None)
