import json
from pathlib import Path
from typing import Any

import psycopg
import pytest

from src.config import Database
from src.pipeline.atp import load_spiders, parse_dated_log
from tests.conftest import one


def test_newest_commit_wins() -> None:
    log = (
        "2026-09-11T19:04:00Z\n\nlocations/spiders/a.py\n"
        "2026-06-30T16:00:27+01:00\n\nlocations/spiders/a.py\nlocations/spiders/b.py\n"
    )
    assert parse_dated_log(log) == {
        "locations/spiders/a.py": "2026-09-11T19:04:00Z",
        "locations/spiders/b.py": "2026-06-30T16:00:27+01:00",
    }


@pytest.mark.parametrize(
    "updated_at",
    [None, "2026-09-10T12:34:56+02:00", "absent"],
    ids=["null", "iso-with-offset", "predates-the-dating"],
)
def test_load_spiders_types_the_date(
    test_db: Database, tmp_path: Path, updated_at: str | None
) -> None:
    """The site compares updated_at to a timestamptz: the file's shape must
    not decide the column's type.
    """
    spider: dict[str, Any] = {
        "spider": "a",
        "filename": "locations/spiders/a.py",
        "errors": 0,
        "features": 3,
        "elapsed_time": 1.5,
    }
    if updated_at != "absent":
        spider["updated_at"] = updated_at
    path = tmp_path / "spiders.json"
    path.write_text(json.dumps([spider]))
    with psycopg.connect(test_db.conninfo) as conn, conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS t")
        load_spiders(cur, path, "t")
        cur.execute("SELECT count(*) FROM t WHERE updated_at > '2026-01-01'::timestamptz")
        assert one(cur.fetchone())[0] == (1 if updated_at == "2026-09-10T12:34:56+02:00" else 0)
        cur.execute("DROP TABLE t")
