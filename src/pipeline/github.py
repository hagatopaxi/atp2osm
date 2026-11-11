"""Refresh the state of the GitHub issues the missing brands are linked to.

A merged pull request is final; an open or closed issue can still move, and so
can one GitHub never answered for. GitHub down costs this run's update, not
the run: a state it does not give is left as it was.
"""

import logging

from src.github_issues import fetch_issue_state
from src.pipeline._db import connect

logger = logging.getLogger(__name__)


def refresh_github_issues() -> None:
    conn = connect()
    try:
        rows = conn.execute(
            """SELECT id, github_issue FROM todo_brands
               WHERE github_issue IS NOT NULL AND issue_state IS DISTINCT FROM 'merged'"""
        ).fetchall()
        refreshed = 0
        for entry_id, url in rows:
            state = fetch_issue_state(url)
            if state is None:
                continue
            conn.execute(
                """UPDATE todo_brands SET issue_state = %s, issue_checked_at = NOW()
                   WHERE id = %s AND github_issue = %s""",
                (state, entry_id, url),
            )
            conn.commit()
            refreshed += 1
        logger.info("Refreshed %d of %d GitHub issue states", refreshed, len(rows))
    finally:
        conn.close()
