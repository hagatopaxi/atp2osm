import psycopg

from src.config import get_database


def connect():
    return psycopg.connect(**get_database().connect_kwargs)


def last_import_date(conn, import_type):
    with conn.cursor() as cur:
        cur.execute(
            # NULLS LAST: pending and error rows carry no date.
            "SELECT date FROM data_imports WHERE type=%s"
            " ORDER BY date DESC NULLS LAST LIMIT 1",
            (import_type,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def last_import_comment(conn, import_type):
    """Comment of the last resolved import — NSI stores its npm version there."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT comment FROM data_imports WHERE type=%s AND status <> 'pending'"
            " ORDER BY created_at DESC LIMIT 1",
            (import_type,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def start_import(conn, import_type):
    """Open the row of this datasource's run: 'pending' until record_import
    resolves it, which the home page shows as syncing. The site keeps serving
    meanwhile — every rebuild swaps its object in at the end, see
    _matview.swap()."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO data_imports (type, date, status) VALUES (%s, NULL, 'pending')",
            (import_type,),
        )
    conn.commit()


def record_import(conn, import_type, date, status, comment=None):
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
                "INSERT INTO data_imports (type, date, status, comment)"
                " VALUES (%s, %s, %s, %s)",
                (import_type, date, status, comment),
            )
    conn.commit()
