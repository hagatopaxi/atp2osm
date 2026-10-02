"""Bridge: build mv_matches where the pipeline has not yet.

/validate reads its matches off mv_matches, which the refresh builds — a
deploy would otherwise leave it missing until the next one. Built from the
code's own SQL, unstamped: the next refresh finds it stale and rebuilds it
with the counters. The join takes minutes on a whole country; the site keeps
serving the previous revision meanwhile.

A database the pipeline never filled has nothing to match: the first refresh
builds the view.
"""

import logging

from psycopg import sql

from src.migrate import Migration
from src.pipeline.atp2osm import MATCHES_INDEXES, mv_matches_sql

logger = logging.getLogger(__name__)


class BuildMatches(Migration):
    def migrate(self) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT to_regclass('mv_matches'), to_regclass('mv_places'),"
                " to_regclass('atp_places')"
            )
            row = cur.fetchone()
            if row is None or row[0] is not None or row[1] is None or row[2] is None:
                return
            logger.info("Building mv_matches, this takes minutes...")
            cur.execute(mv_matches_sql())
            for index, definition in MATCHES_INDEXES.items():
                cur.execute(
                    sql.SQL("CREATE INDEX {} ON mv_matches {}").format(
                        sql.Identifier(index), sql.SQL(definition)
                    )
                )
