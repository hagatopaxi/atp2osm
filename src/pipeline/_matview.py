"""Rebuild-when-needed guard for derived objects.

An object's content depends on two things: the code that builds it, and the
datasources it reads. Both are folded into a signature stamped on the object
itself as a COMMENT, and compared before rebuilding. The code half is a single
value, `_version.app_version()` — see there for why it is not a digest.

This exists because the alternatives both fail:

* rebuilding every night costs a minute for nothing, 364 nights out of 365;
* guarding on the freshness of a single datasource silently freezes the object
  when *another* one moves — mv_places reads nsi_brands as well as the OSM
  tables, and mv_places_brand reads mv_places and atp_places.

Listing the inputs is therefore not optional: an input left out is an update
that never lands. Whatever an object reads, pass it here.
"""

import hashlib

from psycopg import sql


def signature(*inputs) -> str:
    """Signature of everything an object depends on.

    `inputs` is anything that identifies the version of what it reads — the
    deployed revision, an import date, a source version string. None is fine:
    it just means "no data yet", and differs from any later value.
    """
    parts = [str(value) for value in inputs]
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]


def is_current(conn, name: str, sig: str) -> bool:
    """True when `name` exists and was built from this exact signature."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT obj_description(to_regclass(%s), 'pg_class')", (name,)
        )
        row = cur.fetchone()
    return bool(row) and row[0] == sig


def stamp(cur, name: str, sig: str, kind: str = "MATERIALIZED VIEW") -> None:
    """Record the signature on the object, once it is built.

    `kind` is what COMMENT ON needs to name it — a derived plain TABLE is
    guarded exactly like a view.
    """
    # COMMENT ON is a utility statement: it takes no bound parameter, so the
    # value has to be composed in.
    cur.execute(
        sql.SQL("COMMENT ON {} {} IS {}").format(
            sql.SQL(kind), sql.Identifier(name), sql.Literal(sig)
        )
    )
