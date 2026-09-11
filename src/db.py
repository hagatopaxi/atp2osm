import psycopg
from flask import g

from src.config import get_database


def get_osmdb():
    if "osmdb" not in g:
        osmdb = psycopg.connect(**get_database().connect_kwargs)
        g.osmdb = osmdb

    return g.osmdb


def teardown_osmdb(exception):
    osmdb = g.pop("osmdb", None)

    if osmdb is not None:
        osmdb.close()
