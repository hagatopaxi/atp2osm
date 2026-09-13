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

from src.pipeline._db import forced


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
    if forced():
        return False
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


# --- Swapping a rebuilt object in ------------------------------------------
#
# A rebuild never drops the live object first: the site would then hang on the
# exclusive lock for the minutes the build takes, or hit a missing table. The
# object is built beside the live one, under another name, and swapped in at
# the end — the lock is held for the milliseconds of a rename, and a failed
# build leaves the live object exactly as it was.
#
# The live object is renamed away rather than dropped, because a view
# materialized on it keeps its rows and its dependency: mv_places_brand reads
# mv_places, mv_places reads points. Dropping mv_places with CASCADE would
# take the brand view down for the minutes until its own rebuild. The retired
# objects go once nothing depends on them any more, see drop_retired().


def _indexes(cur, oid):
    """(index name, constraint name or None) for every index on `oid`."""
    cur.execute(
        """SELECT c.relname, con.conname
             FROM pg_index i
             JOIN pg_class c ON c.oid = i.indexrelid
             LEFT JOIN pg_constraint con ON con.conindid = i.indexrelid
            WHERE i.indrelid = %s""",
        (oid,),
    )
    return cur.fetchall()


def swap(cur, kind: str, name: str, new: str, indexes=()) -> None:
    """Put `new` in the place of `name`, in the caller's transaction.

    `new` is either `<name>_new`, renamed in, or `<schema>.<name>`, moved into
    the public schema — how osm2pgsql delivers its tables. `indexes` names the
    indexes of the renamed variant: they were created as `<index>_new` and take
    their name back here, since a name is unique per schema and the live
    object's index holds it until now. CREATE INDEX IF NOT EXISTS under the
    final name would have silently created nothing.

    The live object becomes `<name>_old`, stripped of its indexes: nobody reads
    it any more, and a view materialized on it needs none. A `<name>_old`
    still there from a previous run — the brand view hanging on mv_places_old
    after a failed mv-brand — is moved out of the way as `<name>_old_<oid>`,
    kept alive for the same reason.
    """
    old = f"{name}_old"
    cur.execute("SELECT to_regclass(%s)::oid", (old,))
    leftover = cur.fetchone()[0]
    if leftover is not None:
        cur.execute(
            sql.SQL("ALTER {} {} RENAME TO {}").format(
                sql.SQL(kind), sql.Identifier(old), sql.Identifier(f"{old}_{leftover}")
            )
        )

    cur.execute("SELECT to_regclass(%s)::oid", (name,))
    live = cur.fetchone()[0]
    if live is not None:
        for index, constraint in _indexes(cur, live):
            if constraint:
                cur.execute(
                    sql.SQL("ALTER TABLE {} DROP CONSTRAINT {}").format(
                        sql.Identifier(name), sql.Identifier(constraint)
                    )
                )
            else:
                cur.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier(index)))
        cur.execute(
            sql.SQL("ALTER {} {} RENAME TO {}").format(
                sql.SQL(kind), sql.Identifier(name), sql.Identifier(old)
            )
        )

    if "." in new:
        schema, _ = new.split(".", 1)
        cur.execute(
            sql.SQL("ALTER {} {} SET SCHEMA public").format(
                sql.SQL(kind), sql.Identifier(schema, name)
            )
        )
    else:
        cur.execute(
            sql.SQL("ALTER {} {} RENAME TO {}").format(
                sql.SQL(kind), sql.Identifier(new), sql.Identifier(name)
            )
        )
        for index in indexes:
            cur.execute(
                sql.SQL("ALTER INDEX {} RENAME TO {}").format(
                    sql.Identifier(f"{index}_new"), sql.Identifier(index)
                )
            )


def create_indexes(cur, table: str, indexes: dict[str, str]) -> None:
    """Build `indexes` — canonical name to definition — on `table`, a `_new`
    object: each index is named `<name>_new`, and swap() renames it."""
    for index, definition in indexes.items():
        cur.execute(
            sql.SQL("CREATE INDEX {} ON {} {}").format(
                sql.Identifier(f"{index}_new"), sql.Identifier(table), sql.SQL(definition)
            )
        )


def drop_retired(cur, kind: str, name: str) -> list[str]:
    """Drop every `<name>_old*` nothing depends on any more; return the names.

    No CASCADE: a retired object something still reads is kept, never taken
    down with its reader. That happens when a rebuild upstream was not
    followed by one downstream — points reimported from a PBF a crashed run
    left behind, mv_places then current and still built on points_old. The
    next rebuild of the reader frees it, and this drops it then.
    """
    cur.execute(
        """SELECT c.relname
             FROM pg_class c
            WHERE c.relnamespace = 'public'::regnamespace
              AND c.relkind IN ('r', 'm')
              AND starts_with(c.relname, %s)
              AND NOT EXISTS (SELECT 1 FROM pg_depend d
                               WHERE d.refclassid = 'pg_class'::regclass
                                 AND d.refobjid = c.oid
                                 AND d.deptype = 'n')
            ORDER BY c.relname""",
        (f"{name}_old",),
    )
    dropped = [row[0] for row in cur.fetchall()]
    for retired in dropped:
        cur.execute(
            sql.SQL("DROP {} {}").format(sql.SQL(kind), sql.Identifier(retired))
        )
    return dropped
