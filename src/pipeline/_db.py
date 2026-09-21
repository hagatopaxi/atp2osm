import os
from datetime import datetime
from typing import Any

import psycopg

from src.config import get_database

Connection = psycopg.Connection[Any]


def forced() -> bool:
    """ATP2OSM_FORCE=1: every step rebuilds as if nothing had ever been
    imported. Every guard reads the previous run through the helpers below
    or through _matview.is_current, so this is the one place to lie.
    """
    return bool(os.environ.get("ATP2OSM_FORCE"))


def connect() -> Connection:
    return psycopg.connect(get_database().conninfo)


def last_import_date(conn: Connection, import_type: str) -> datetime | None:
    if forced():
        return None
    with conn.cursor() as cur:
        cur.execute(
            # NULLS LAST: pending and error rows carry no date.
            "SELECT date FROM data_imports WHERE type=%s ORDER BY date DESC NULLS LAST LIMIT 1",
            (import_type,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def last_import_comment(conn: Connection, import_type: str) -> str | None:
    """Comment of the last resolved import — NSI stores its npm version there."""
    if forced():
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT comment FROM data_imports WHERE type=%s AND status <> 'pending'"
            " ORDER BY created_at DESC LIMIT 1",
            (import_type,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def start_import(conn: Connection, import_type: str) -> None:
    """Open the row of this datasource's run: 'pending' until record_import
    resolves it, which the home page shows as syncing. The site keeps serving
    meanwhile — every rebuild swaps its object in at the end, see
    _matview.swap().
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_imports (type, date, status) VALUES (%s, NULL, 'pending')",
            (import_type,),
        )
    conn.commit()


def record_import(
    conn: Connection,
    import_type: str,
    date: datetime | None,
    status: str,
    comment: str | None = None,
) -> None:
    """Resolve the row start_import opened, or insert one if there is none —
    the shared steps ('pipeline') never open one, and a step failing after its
    branch already recorded its result finds it closed.
    """
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE data_imports SET date=%s, status=%s, comment=%s, created_at=NOW()
               WHERE id = (SELECT id FROM data_imports
                           WHERE type=%s AND status='pending'
                           ORDER BY created_at DESC LIMIT 1)""",
            (date, status, comment, import_type),
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO data_imports (type, date, status, comment) VALUES (%s, %s, %s, %s)",
                (import_type, date, status, comment),
            )
    conn.commit()
