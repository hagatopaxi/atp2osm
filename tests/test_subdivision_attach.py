"""Spatial attachment of a POI to its administrative subdivision.

Needs the local PostGIS (podman-compose up -d); skipped otherwise. The
subdivisions table is written by hand in a throwaway schema: what is under test
is the query, not osm2pgsql.

The geometry is a grid of unit squares, so every case can be read off the
coordinates. The country (level 2) is the 0..10 square; everything else is
carved out of it.

The attachment reads subdivision_parts, the boundaries cut into index-sized
pieces by the OSM import; the fixture builds it the same way, so the cutting is
part of what is under test.
"""

import psycopg
import pytest
from psycopg.rows import dict_row

from src.pipeline.atp import _attach_subdivisions

SCHEMA = "test_subdivisions"

# osm_id, name, ref, admin_level, (x0, y0, x1, y1). The id is explicit so that
# shuffling the rows changes only their physical order, never the data.
BOUNDARIES = [
    # The country: nothing outside it is attachable.
    (1, "France", "FR", 2, (0, 0, 10, 10)),
    # A neighbour clipped into the same extract: it must never win.
    (13, "Monaco", "MC", 2, (11, 11, 12, 12)),
    # Métropole: a level 4 region holding two level 6 départements.
    (2, "Provence-Alpes-Cote d'Azur", "93", 4, (0, 0, 2, 1)),
    (3, "Bouches-du-Rhone", "13", 6, (0, 0, 1, 1)),
    (4, "Var", "83", 6, (1, 0, 2, 1)),
    # Guadeloupe: has both levels, the finest must win.
    (5, "Guadeloupe (region)", "01", 4, (3, 0, 4, 1)),
    (6, "Guadeloupe", "971", 6, (3, 0, 4, 1)),
    # Martinique and Guyane: level 4 only, no 6 exists.
    (7, "Martinique", "972R", 4, (5, 0, 6, 1)),
    (8, "Guyane", "973R", 4, (6, 0, 7, 1)),
    # Nouvelle-Caledonie: a level 3 territory split into level 4 provinces.
    (9, "Nouvelle-Caledonie", "988", 3, (0, 3, 2, 4)),
    (10, "Province Sud", None, 4, (0, 3, 1, 4)),
    # Polynesie and Wallis: level 3 and nothing below.
    (11, "Polynesie francaise", "987", 3, (3, 3, 4, 4)),
    (12, "Wallis-et-Futuna", "986", 3, (5, 3, 6, 4)),
]


def _bbox(x0, y0, x1, y1):
    return (
        f"SRID=4326;POLYGON(({x0} {y0}, {x1} {y0}, {x1} {y1}, {x0} {y1}, {x0} {y0}))"
    )


@pytest.fixture
def conn(db_kwargs):
    with psycopg.connect(row_factory=dict_row, **db_kwargs) as c:
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.execute(f"CREATE SCHEMA {SCHEMA}")
        c.execute(f"SET search_path TO {SCHEMA}, public")
        c.execute("""
            CREATE TABLE subdivisions (
                -- osm2pgsql's define_area_table also adds an area_id of its
                -- own; the attachment never reads it.
                area_id     BIGSERIAL,
                osm_id      BIGINT PRIMARY KEY,
                ref         TEXT,
                name        TEXT NOT NULL,
                admin_level INT  NOT NULL,
                geom        GEOMETRY(Polygon, 4326) NOT NULL
            )
        """)
        c.commit()
        yield c
        c.rollback()
        c.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        c.commit()


def load_boundaries(conn, boundaries=BOUNDARIES):
    cur = conn.cursor()
    cur.execute("TRUNCATE subdivisions")
    cur.executemany(
        "INSERT INTO subdivisions (osm_id, ref, name, admin_level, geom)"
        " VALUES (%s, %s, %s, %s, %s)",
        [
            (osm_id, ref, name, level, _bbox(*box))
            for osm_id, name, ref, level, box in boundaries
        ],
    )
    conn.commit()
    build_parts(conn)


def build_parts(conn):
    """Exactly what the OSM import builds, cut small enough that a piece has to
    be stitched back to its subdivision. Rebuilt whenever subdivisions moves."""
    cur = conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.subdivision_parts")
    cur.execute(f"""
        CREATE TABLE {SCHEMA}.subdivision_parts AS
        SELECT osm_id, ref, name, admin_level, ST_Subdivide(geom, 8) AS geom
          FROM subdivisions
    """)
    cur.execute(f"CREATE INDEX ON {SCHEMA}.subdivision_parts USING GIST (geom)")
    conn.commit()


def attach(conn, points):
    """Run the real attachment on a hand-made atp_places and return its rows."""
    with conn.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS {SCHEMA}.atp_places")
        cur.execute(f"CREATE TABLE {SCHEMA}.atp_places (id TEXT PRIMARY KEY, geom TEXT)")
        cur.executemany(
            f"INSERT INTO {SCHEMA}.atp_places (id, geom) VALUES (%s, ST_AsGeoJSON(ST_Point(%s, %s)))",
            [(name, x, y) for name, (x, y) in points.items()],
        )
    conn.commit()

    _attach_subdivisions(conn)

    with conn.cursor() as cur:
        rows = cur.execute(
            f"SELECT id, subdivision_code, subdivision_name FROM {SCHEMA}.atp_places"
        ).fetchall()
    return {r["id"]: (r["subdivision_code"], r["subdivision_name"]) for r in rows}


def test_one_point_per_level_reached(conn):
    """Every territory keeps POIs, whichever level it bottoms out at."""
    load_boundaries(conn)
    got = attach(conn, {
        "metropole":   (0.5, 0.5),   # level 6
        "guadeloupe":  (3.5, 0.5),   # level 6, under a level 4
        "martinique":  (5.5, 0.5),   # level 4, no level 6 exists
        "guyane":      (6.5, 0.5),   # level 4
        "caledonie":   (0.5, 3.5),   # level 4 province inside a level 3
        "polynesie":   (3.5, 3.5),   # level 3
        "wallis":      (5.5, 3.5),   # level 3
    })

    assert got == {
        "metropole":  ("13", "Bouches-du-Rhone"),
        "guadeloupe": ("971", "Guadeloupe"),
        "martinique": ("972R", "Martinique"),
        "guyane":     ("973R", "Guyane"),
        "caledonie":  ("10", "Province Sud"),  # no ref: falls back on osm_id
        "polynesie":  ("987", "Polynesie francaise"),
        "wallis":     ("986", "Wallis-et-Futuna"),
    }


def test_finest_level_wins_whatever_the_insertion_order(conn):
    """A point covered by a 6 and a 4 gets the 6, both ways round."""
    load_boundaries(conn, list(reversed(BOUNDARIES)))
    assert attach(conn, {"p": (3.5, 0.5)})["p"] == ("971", "Guadeloupe")


def test_a_point_outside_the_country_is_rejected(conn):
    """Level 2 covers the whole country: no attachment means no country."""
    load_boundaries(conn)
    got = attach(conn, {"inside": (0.5, 0.5), "abroad": (42.0, 42.0)})
    assert "abroad" not in got
    assert "inside" in got


def test_a_neighbour_in_the_same_extract_keeps_its_own_pois(conn):
    """Geofabrik carries the neighbours: a POI inside Monaco lands in Monaco.

    Accepted as a side effect rather than filtered out — it is a handful of
    rows, and they keep a name a reviewer can read.
    """
    load_boundaries(conn)
    assert attach(conn, {"p": (11.5, 11.5)}) == {"p": ("MC", "Monaco")}


def test_a_point_in_the_country_but_in_no_subdivision_falls_back_on_it(conn):
    """The sea inside the territorial waters, a gap between two polygons."""
    load_boundaries(conn)
    assert attach(conn, {"p": (7.5, 7.5)})["p"] == ("FR", "France")


def test_an_enclave_wins_over_the_polygon_around_it(conn):
    """A hole in a boundary is a real one: the enclave is not overlapped."""
    load_boundaries(conn)
    conn.cursor().execute(
        """UPDATE subdivisions
              SET geom = ST_Difference(geom, ST_GeomFromText(
                  'POLYGON((0.2 0.2, 0.4 0.2, 0.4 0.4, 0.2 0.4, 0.2 0.2))', 4326))
            WHERE ref = '13'"""
    )
    conn.cursor().execute(
        "INSERT INTO subdivisions (osm_id, ref, name, admin_level, geom)"
        " VALUES (99, '99', 'Enclave', 6, %s)",
        (_bbox(0.2, 0.2, 0.4, 0.4),),
    )
    conn.commit()
    build_parts(conn)

    assert attach(conn, {"p": (0.3, 0.3)})["p"] == ("99", "Enclave")
    assert attach(conn, {"p": (0.6, 0.6)})["p"] == ("13", "Bouches-du-Rhone")


def test_two_subdivisions_of_the_same_level_overlapping_is_settled_by_osm_id(conn):
    """It should not happen; if it does, the answer must not be row order."""
    load_boundaries(conn, BOUNDARIES + [(99, "Doublon", "99", 6, (0.2, 0.2, 0.4, 0.4))])
    first = attach(conn, {"p": (0.3, 0.3)})["p"]
    load_boundaries(conn, list(reversed(BOUNDARIES + [(99, "Doublon", "99", 6, (0.2, 0.2, 0.4, 0.4))])))
    assert attach(conn, {"p": (0.3, 0.3)})["p"] == first


def test_a_point_on_a_shared_border_is_attached_deterministically(conn):
    """ST_Intersects includes the boundary, so a border POI keeps a fine level.

    Two subdivisions claim it; what matters is that the answer never depends on
    the row order.
    """
    load_boundaries(conn)
    first = attach(conn, {"p": (1.0, 0.5)})["p"]
    load_boundaries(conn, list(reversed(BOUNDARIES)))
    assert attach(conn, {"p": (1.0, 0.5)})["p"] == first
