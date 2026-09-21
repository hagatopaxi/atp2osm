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


def code_sql(query: str) -> sql.SQL:
    """A query assembled from code constants alone, vouched for as such.

    psycopg only takes literal SQL, so that a value never reaches a query
    outside a bound parameter. What is composed here is composed from module
    constants or values the caller has validated — the cooldowns are checked
    at import time, the calling codes against a regex — and this is where
    that is stated.
    """
    return sql.SQL(cast("LiteralString", query))
