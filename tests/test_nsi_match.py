"""nsi_match() runs in PostgreSQL, so this test needs the real database."""

import pytest

from tests.conftest import Connection, one

QID = "Q-test-nsi-match"


@pytest.fixture
def conn(migrated_conn: Connection) -> Connection:
    migrated_conn.execute(
        "INSERT INTO nsi_brands"
        " (brand_wikidata, brand, name, primary_key, primary_value, tags)"
        " VALUES (%s, 'Test', 'Test', 'amenity', 'fuel', %s)",
        (QID, '{"amenity": "fuel", "operator:wikidata": "Q-op"}'),
    )
    return migrated_conn


def match(conn: Connection, tags: str) -> dict[str, str] | None:
    return one(conn.execute("SELECT nsi_match(%s::jsonb)", (tags,)).fetchone())[0]


def test_applies_to_an_object_of_the_same_category(conn: Connection) -> None:
    got = match(conn, f'{{"amenity": "fuel", "brand:wikidata": "{QID}"}}')
    assert got == {"amenity": "fuel", "operator:wikidata": "Q-op"}


def test_applies_to_an_object_without_a_primary_tag(conn: Connection) -> None:
    got = match(conn, f'{{"brand:wikidata": "{QID}"}}')
    assert got == {"amenity": "fuel", "operator:wikidata": "Q-op"}


def test_never_reclassifies_an_object_of_another_category(conn: Connection) -> None:
    # way/130021335: a Casino supermarket carrying the QID of Casino's fuel
    # stations must not come out as amenity=fuel.
    got = match(conn, f'{{"shop": "supermarket", "brand:wikidata": "{QID}"}}')
    assert got == {"operator:wikidata": "Q-op"}


def test_recovers_the_qid_from_the_name(conn: Connection) -> None:
    got = match(conn, '{"amenity": "fuel", "brand": "Test"}')
    assert got == {"amenity": "fuel", "operator:wikidata": "Q-op"}


def test_never_recovers_a_denied_qid_from_the_name(conn: Connection) -> None:
    # node/5037224542: a contributor wrote not:brand:wikidata on the object,
    # the name must not bring the same QID back.
    got = match(conn, f'{{"amenity": "fuel", "brand": "Test", "not:brand:wikidata": "{QID}"}}')
    assert got is None
