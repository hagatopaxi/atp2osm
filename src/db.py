from pathlib import Path
from typing import LiteralString, cast

import psycopg
from flask import g
from psycopg import sql
from psycopg.rows import TupleRow

from src.config import get_database


def get_osmdb() -> psycopg.Connection[TupleRow]:
    if "osmdb" not in g:
        g.osmdb = psycopg.connect(get_database().conninfo)
    return g.osmdb


def teardown_osmdb(_exception: BaseException | None) -> None:
    osmdb = g.pop("osmdb", None)

    if osmdb is not None:
        osmdb.close()


def sql_file(path: Path) -> sql.SQL:
    """The SQL a file of the repository holds, run as it is written.

    psycopg only takes literal SQL, so that a value never reaches a query
    outside a bound parameter, and pyright proves it everywhere else. A
    migration or a function definition is versioned code, reviewed like the
    Python beside it: nothing a request carries gets into it without a commit.
    Only a `Path` is taken, so a string built at run time cannot come through.
    """
    return sql.SQL(cast("LiteralString", path.read_text(encoding="utf-8")))
