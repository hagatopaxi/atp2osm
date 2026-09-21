import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import psycopg
import pytest
from psycopg import sql

from src.config import Database
from src.migrate import Migration, discover_migrations, run_migrations, run_python_migration
from tests.conftest import Connection, one

# What the migration under test is handed: a list, to see what it does with it.
fake_conn = cast("Connection", [])

# The "connection" is a list here, to see what the migration does with it.
ONE_CLASS = """
from src.migrate import Migration


class Bump(Migration):
    def migrate(self):
        self.conn.append("ran")
"""

NO_CLASS = "x = 1\n"

TWO_CLASSES = """
from src.migrate import Migration


class A(Migration):
    def migrate(self):
        pass


class B(Migration):
    def migrate(self):
        pass
"""


def write(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


def test_runs_the_migration_class_with_the_connection(tmp_path: Path) -> None:
    ran: list[str] = []
    run_python_migration(write(tmp_path, "100_bump.py", ONE_CLASS), cast("Connection", ran))
    assert ran == ["ran"]


def test_refuses_a_file_without_migration_class(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exactly one"):
        run_python_migration(write(tmp_path, "100_none.py", NO_CLASS), fake_conn)


def test_refuses_a_file_with_two_migration_classes(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="exactly one"):
        run_python_migration(write(tmp_path, "100_two.py", TWO_CLASSES), fake_conn)


def test_base_class_refuses_to_run_on_its_own() -> None:
    with pytest.raises(NotImplementedError):
        Migration(fake_conn).migrate()


def test_discovers_both_sql_and_python_migrations_in_order() -> None:
    found = discover_migrations()
    versions = [v for v, _ in found]
    assert versions == sorted(versions)
    suffixes = {p.suffix for _, p in found}
    assert suffixes == {".sql", ".py"}
    # the backfill is a Python migration, and it comes before dropping the
    # column it reads
    by_version = dict(found)
    assert by_version[16].suffix == ".py"
    assert by_version[17].suffix == ".sql"


# --- Applying them, on the throwaway database ----------------------------------------


@pytest.fixture
def migrations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A migrations directory of the test's own."""
    from src import migrate

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def bare(test_db: Database) -> Iterator[Connection]:
    """A connection on a database of this test's own: the shared one carries
    the real migration record, which these must neither read nor drop.
    """
    name = f"{test_db.name}_migrate"
    with psycopg.connect(test_db.conninfo, autocommit=True) as admin:
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(dataclasses.replace(test_db, name=name).conninfo) as conn:
        yield conn
    with psycopg.connect(test_db.conninfo, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def applied(conn: Connection) -> list[str]:
    conn.rollback()
    return [
        str(r[0]) for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY version")
    ]


def test_migrations_are_applied_in_order_and_recorded(migrations: Path, bare: Connection) -> None:
    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    write(migrations, "002_second.sql", "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES (1)")

    run_migrations(bare)

    assert applied(bare) == ["001_first.sql", "002_second.sql"]
    assert bare.execute("SELECT x FROM m2").fetchone() == (1,)


def test_a_second_run_applies_nothing(migrations: Path, bare: Connection) -> None:
    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    run_migrations(bare)
    run_migrations(bare)  # CREATE TABLE would fail if replayed
    assert applied(bare) == ["001_first.sql"]


def test_a_failing_migration_is_rolled_back_not_recorded_and_stops_the_deploy(
    migrations: Path, bare: Connection
) -> None:
    """The ones before it stay applied; the broken one is retried once
    fixed, and the ones after it wait.
    """
    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    write(
        migrations,
        "002_broken.sql",
        "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES ('not a number')",
    )
    write(migrations, "003_third.sql", "CREATE TABLE m3 (x INT)")

    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        run_migrations(bare)

    assert applied(bare) == ["001_first.sql"]
    assert bare.execute("SELECT to_regclass('m2'), to_regclass('m3')").fetchone() == (None, None)

    write(migrations, "002_broken.sql", "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES (2)")
    run_migrations(bare)
    assert applied(bare) == ["001_first.sql", "002_broken.sql", "003_third.sql"]


def test_a_python_migration_runs_on_the_connection(migrations: Path, bare: Connection) -> None:
    write(
        migrations,
        "001_py.py",
        """
from src.migrate import Migration


class Bump(Migration):
    def migrate(self):
        self.conn.execute("CREATE TABLE m1 (x INT)")
""",
    )
    run_migrations(bare)
    assert one(bare.execute("SELECT to_regclass('m1')").fetchone())[0] is not None
    assert applied(bare) == ["001_py.py"]
