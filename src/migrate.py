import importlib.util
import inspect
import logging
import pathlib
import re

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).parent.parent / "migrations"


class Migration:
    """Base class for .py migrations — those that need more than SQL.

    The runner instantiates the subclass found in the file with the open
    connection and calls migrate(). Commit and rollback stay its business.
    """

    def __init__(self, conn):
        self.conn = conn

    def migrate(self):
        raise NotImplementedError


def _ensure_schema_migrations_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            filename    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)


def _get_applied_versions(cursor):
    cursor.execute("SELECT version FROM schema_migrations ORDER BY version;")
    return {row[0] for row in cursor.fetchall()}


def _discover_migrations():
    """Return sorted list of (version, filepath) from the migrations directory."""
    pattern = re.compile(r"^(\d+)_.+\.(sql|py)$")
    migrations = []

    if not MIGRATIONS_DIR.is_dir():
        logger.warning(f"Migrations directory not found: {MIGRATIONS_DIR}")
        return migrations

    for path in sorted(MIGRATIONS_DIR.iterdir()):
        match = pattern.match(path.name)
        if match:
            version = int(match.group(1))
            migrations.append((version, path))

    return migrations


def _run_python_migration(path, conn):
    """Load the file, find its Migration subclass, run migrate()."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    subclasses = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Migration) and obj is not Migration
    ]
    if len(subclasses) != 1:
        raise RuntimeError(
            f"{path.name} must define exactly one Migration subclass, found {len(subclasses)}"
        )

    subclasses[0](conn).migrate()


def run_migrations(conn):
    """Run all pending SQL migrations. Called at server startup."""
    logger.info("Checking for pending migrations...")

    with conn.cursor() as cursor:
        _ensure_schema_migrations_table(cursor)
        conn.commit()

        # Every gunicorn worker runs this at boot: the lock serialises them, so
        # the second one reads the first one's rows instead of racing it.
        # Session level, so it survives the commit after each migration; it
        # goes with the connection.
        cursor.execute("SELECT pg_advisory_lock(hashtext('schema_migrations'));")
        applied = _get_applied_versions(cursor)
        migrations = _discover_migrations()
        pending = [(v, p) for v, p in migrations if v not in applied]

        if not pending:
            logger.info("No pending migrations.")
            return

        logger.info(f"{len(pending)} pending migration(s) to apply.")

        for version, path in pending:
            logger.info(f"Applying migration {path.name}...")
            try:
                if path.suffix == ".py":
                    _run_python_migration(path, conn)
                else:
                    cursor.execute(path.read_text(encoding="utf-8"))
                cursor.execute(
                    "INSERT INTO schema_migrations (version, filename) VALUES (%s, %s);",
                    (version, path.name),
                )
                conn.commit()
                logger.info(f"Migration {path.name} applied successfully.")
            except Exception:
                conn.rollback()
                logger.exception(f"Migration {path.name} failed.")
                raise

    logger.info("All migrations applied.")
