"""nsi_match() runs in PostgreSQL, so this test needs the real database."""

import psycopg
import pytest

QID = "Q-test-nsi-match"


@pytest.fixture
def conn(migrated_conn):
    migrated_conn.execute(
        "INSERT INTO nsi_brands"
        " (brand_wikidata, brand, name, primary_key, primary_value, tags)"
        " VALUES (%s, 'Test', 'Test', 'amenity', 'fuel', %s)",
        (QID, '{"amenity": "fuel", "operator:wikidata": "Q-op"}'),
    )
    return migrated_conn


def match(conn, tags):
    return conn.execute("SELECT nsi_match(%s::jsonb)", (tags,)).fetchone()[0]


def test_applies_to_an_object_of_the_same_category(conn):
    got = match(conn, '{"amenity": "fuel", "brand:wikidata": "%s"}' % QID)
    assert got == {"amenity": "fuel", "operator:wikidata": "Q-op"}


def test_applies_to_an_object_without_a_primary_tag(conn):
    got = match(conn, '{"brand:wikidata": "%s"}' % QID)
    assert got == {"amenity": "fuel", "operator:wikidata": "Q-op"}


def test_never_reclassifies_an_object_of_another_category(conn):
    # way/130021335: a Casino supermarket carrying the QID of Casino's fuel
    # stations must not come out as amenity=fuel.
    got = match(conn, '{"shop": "supermarket", "brand:wikidata": "%s"}' % QID)
    assert got == {"operator:wikidata": "Q-op"}
