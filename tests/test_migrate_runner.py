import pytest

from src.migrate import Migration, _discover_migrations, _run_python_migration

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


def write(tmp_path, name, source):
    path = tmp_path / name
    path.write_text(source)
    return path


def test_runs_the_migration_class_with_the_connection(tmp_path):
    conn = []
    _run_python_migration(write(tmp_path, "100_bump.py", ONE_CLASS), conn)
    assert conn == ["ran"]


def test_refuses_a_file_without_migration_class(tmp_path):
    with pytest.raises(RuntimeError, match="exactly one"):
        _run_python_migration(write(tmp_path, "100_none.py", NO_CLASS), None)


def test_refuses_a_file_with_two_migration_classes(tmp_path):
    with pytest.raises(RuntimeError, match="exactly one"):
        _run_python_migration(write(tmp_path, "100_two.py", TWO_CLASSES), None)


def test_base_class_refuses_to_run_on_its_own():
    with pytest.raises(NotImplementedError):
        Migration(None).migrate()


def test_discovers_both_sql_and_python_migrations_in_order():
    found = _discover_migrations()
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
def migrations(tmp_path, monkeypatch):
    """A migrations directory of the test's own."""
    from src import migrate

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def bare(db_kwargs):
    """A connection on a database of this test's own: the shared one carries
    the real migration record, which these must neither read nor drop."""
    import psycopg

    name = f"{db_kwargs['dbname']}_migrate"
    with psycopg.connect(**db_kwargs, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{name}"')
    with psycopg.connect(**{**db_kwargs, "dbname": name}) as conn:
        yield conn
    with psycopg.connect(**db_kwargs, autocommit=True) as admin:
        admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


def applied(conn):
    conn.rollback()
    return [r[0] for r in conn.execute("SELECT filename FROM schema_migrations ORDER BY version")]


def test_migrations_are_applied_in_order_and_recorded(migrations, bare):
    from src.migrate import run_migrations

    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    write(migrations, "002_second.sql", "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES (1)")

    run_migrations(bare)

    assert applied(bare) == ["001_first.sql", "002_second.sql"]
    assert bare.execute("SELECT x FROM m2").fetchone() == (1,)


def test_a_second_run_applies_nothing(migrations, bare):
    from src.migrate import run_migrations

    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    run_migrations(bare)
    run_migrations(bare)  # CREATE TABLE would fail if replayed
    assert applied(bare) == ["001_first.sql"]


def test_a_failing_migration_is_rolled_back_not_recorded_and_stops_the_deploy(migrations, bare):
    """The ones before it stay applied; the broken one is retried once
    fixed, and the ones after it wait."""
    from src.migrate import run_migrations

    write(migrations, "001_first.sql", "CREATE TABLE m1 (x INT)")
    write(migrations, "002_broken.sql", "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES ('not a number')")
    write(migrations, "003_third.sql", "CREATE TABLE m3 (x INT)")

    with pytest.raises(Exception):
        run_migrations(bare)

    assert applied(bare) == ["001_first.sql"]
    assert bare.execute("SELECT to_regclass('m2'), to_regclass('m3')").fetchone() == (None, None)

    write(migrations, "002_broken.sql", "CREATE TABLE m2 (x INT); INSERT INTO m2 VALUES (2)")
    run_migrations(bare)
    assert applied(bare) == ["001_first.sql", "002_broken.sql", "003_third.sql"]


def test_a_python_migration_runs_on_the_connection(migrations, bare):
    from src.migrate import run_migrations

    write(migrations, "001_py.py", """
from src.migrate import Migration


class Bump(Migration):
    def migrate(self):
        self.conn.execute("CREATE TABLE m1 (x INT)")
""")
    run_migrations(bare)
    assert bare.execute("SELECT to_regclass('m1')").fetchone()[0] is not None
    assert applied(bare) == ["001_py.py"]
