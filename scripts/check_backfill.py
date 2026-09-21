"""Dress rehearsal of the backfill (migration 016) on production data.

    uv run --env-file .env python scripts/check_backfill.py \
        data/prod-history.sql data/prod-logs [--keep]

Loads the import_history dump into a throwaway database, applies 015 then 016,
prints a report and touches nothing else. With --keep the database is kept for
manual inspection; without it, it is dropped. The report says what was
detailed, what was not and why, and above all the discrepancies between the
existing history and what the backfill derives from it.
"""

import collections
import dataclasses
import importlib.util
import os
import pathlib
import subprocess
import sys
from typing import Any, LiteralString

import psycopg
from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from src.config import Database, get_database
from src.migrate import Migration
from src.utils import determine_import_status

MIGRATIONS = pathlib.Path(__file__).parent.parent / "migrations"
CHECK_DB = "atp2osm_backfill_check"


def psql(db: Database, statements: str) -> None:
    """Run SQL without ever putting the password on a command line."""
    env = {**os.environ, "PGPASSWORD": db.password}
    cmd = [
        "psql",
        "-v",
        "ON_ERROR_STOP=1",
        "-q",
        "-h",
        db.host,
        "-p",
        db.port,
        "-U",
        db.user,
        "-d",
        db.name,
        "-f",
        "-",
    ]
    # A fixed command line, no shell; psql is on the PATH of whoever runs this.
    done = subprocess.run(cmd, input=statements, text=True, env=env, check=False)  # noqa: S603
    if done.returncode:
        raise SystemExit(f"psql failed (exit code {done.returncode})")


def _admin() -> psycopg.Connection[Any]:
    admin = dataclasses.replace(get_database(), name="postgres")
    return psycopg.connect(admin.conninfo, autocommit=True)


def _load_backfill() -> type[Migration]:
    """The Migration subclass of migration 016, loaded from its file."""
    spec = importlib.util.spec_from_file_location(
        "backfill", MIGRATIONS / "016_backfill_import_departements.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("migration 016 cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    migration: type[Migration] = module.BackfillImportDepartements
    return migration


def main(dump: str, logs_dir: pathlib.Path, *, keep: bool = False) -> None:
    with _admin() as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(CHECK_DB)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(CHECK_DB)))

    db = dataclasses.replace(get_database(), name=CHECK_DB)
    try:
        # the dump comes from another server: its OWNER/GRANT do not apply
        statements = "\n".join(
            line
            for line in pathlib.Path(dump).read_text().splitlines()
            if not line.startswith(
                ("ALTER TABLE public.import_history OWNER", "ALTER SEQUENCE", "GRANT ", "REVOKE ")
            )
            and "OWNER TO" not in line
        )
        psql(db, statements)
        psql(db, (MIGRATIONS / "015_create_import_departements.sql").read_text())

        os.environ["ATP2OSM_LOGS_DIR"] = str(logs_dir)
        backfill = _load_backfill()

        with psycopg.connect(db.conninfo) as conn:
            backfill(conn).migrate()
            report(conn)
            if keep:
                conn.commit()
            else:
                conn.rollback()
    finally:
        if keep:
            print(
                f"\nDatabase kept. To inspect it:\n"
                f"  psql -h {db.host} -p {db.port} -U {db.user} -d {CHECK_DB}\n"
                f"To drop it:\n"
                f"  dropdb -h {db.host} -p {db.port} -U {db.user} {CHECK_DB}"
            )
        else:
            with _admin() as conn:
                conn.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(CHECK_DB)))


def report(conn: psycopg.Connection[Any]) -> None:
    def q(query: LiteralString, *params: object) -> list[tuple[Any, ...]]:
        return conn.execute(query, params).fetchall()

    total = q("SELECT COUNT(*) FROM import_history")[0][0]
    detailed = q("SELECT COUNT(DISTINCT import_id) FROM import_departements")[0][0]
    lines = q("SELECT COUNT(*) FROM import_departements")[0][0]

    print(f"\n{'=' * 70}\nBACKFILL REPORT\n{'=' * 70}")
    print(f"{total} integration(s) in the history")
    print(f"{detailed} detailed, i.e. {lines} import_departements row(s)")

    print("\nStatuses (integration):")
    for status, n, avec in q(
        """SELECT ih.status, COUNT(*),
                  COUNT(*) FILTER (WHERE EXISTS (
                      SELECT 1 FROM import_departements d WHERE d.import_id = ih.id))
           FROM import_history ih GROUP BY ih.status ORDER BY 2 DESC"""
    ):
        print(f"  {status:<16} {n:>4}  of which {avec:>4} detailed")

    print("\nStatuses (département):")
    for status, n in q(
        "SELECT status, COUNT(*) FROM import_departements GROUP BY status ORDER BY 2 DESC"
    ):
        print(f"  {status:<16} {n:>4}")

    # Discrepancy 1: the status derived from the children must match the stored
    # status (up to the suffixes, which migration 018 drops).
    print("\nDiscrepancies between the stored status and the derived one:")
    children: collections.defaultdict[int, list[dict[str, str]]] = collections.defaultdict(list)
    for import_id, status in q("SELECT import_id, status FROM import_departements"):
        children[import_id].append({"status": status})
    mismatches = 0
    for import_id, status in q("SELECT id, status FROM import_history ORDER BY id"):
        if import_id not in children:
            continue
        derived = determine_import_status(children[import_id])
        if derived != status.split("_")[0]:
            mismatches += 1
            print(f"  #{import_id}: stored {status}, derived {derived}")
    print(f"  {mismatches} discrepancy(ies)")

    # Discrepancy 2: the POIs announced by the history and those of the
    # successful départements must coincide.
    print("\nDiscrepancies on items_count:")
    rows = q(
        """SELECT ih.id, ih.items_count,
                  COALESCE(SUM(d.items_count) FILTER (WHERE d.status = 'success'), 0)
           FROM import_history ih
           JOIN import_departements d ON d.import_id = ih.id
           GROUP BY ih.id, ih.items_count
           HAVING ih.items_count IS DISTINCT FROM
                  COALESCE(SUM(d.items_count) FILTER (WHERE d.status = 'success'), 0)
           ORDER BY ih.id"""
    )
    for import_id, stored, computed in rows:
        print(f"  #{import_id}: history {stored}, départements {computed}")
    print(f"  {len(rows)} discrepancy(ies)")

    # What stays without detail, and why. A cancellation and an empty
    # integration have nothing to detail: the others are the ones that cost.
    print("\nIntegrations without detail, by reason:")
    for reason, n in q(
        """SELECT CASE
                    WHEN status = 'cancelled'  THEN 'cancelled (nothing to detail)'
                    WHEN items_count = 0       THEN 'no POI to integrate'
                    ELSE 'log missing or unusable'
                  END, COUNT(*)
           FROM import_history ih
           WHERE NOT EXISTS (SELECT 1 FROM import_departements d WHERE d.import_id = ih.id)
           GROUP BY 1 ORDER BY 2 DESC"""
    ):
        print(f"  {reason:<32} {n:>4}")
    print("\n  Detail of the real losses:")
    for import_id, status, items, brand, date in q(
        """SELECT id, status, items_count, brand_wikidata, import_date::date
           FROM import_history ih
           WHERE NOT EXISTS (SELECT 1 FROM import_departements d WHERE d.import_id = ih.id)
             AND status <> 'cancelled' AND items_count IS DISTINCT FROM 0
           ORDER BY id"""
    ):
        print(f"    #{import_id:<4} {status:<16} {items!s:>5} POIs  {brand} {date}")
    kept = q(
        """SELECT COUNT(*) FROM import_history
           WHERE comment LIKE '%%Changesets : %%'"""
    )[0][0]
    print(f"  of which {kept} keep their changesets in the comment")

    orphans = q(
        """SELECT COUNT(*) FROM import_departements
           WHERE status <> 'success' AND osm_changeset_id IS NULL"""
    )[0][0]
    print(f"\n{orphans} failed département(s) with no changeset (creation failed)")
    print("=" * 70)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    main(sys.argv[1], pathlib.Path(sys.argv[2]).resolve(), keep="--keep" in sys.argv)
