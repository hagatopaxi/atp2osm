from datetime import UTC, datetime

import pytest

from src.pipeline.atp import Run, select_run
from tests.conftest import one


def run(day: int, **kw: str | None) -> Run:
    return {
        "run_id": f"2026-08-{day:02d}",
        "end_time": f"2026-08-{day:02d}T04:00:00Z",
        "parquet_url": "https://example/latest.parquet",
        "output_url": "https://example/output.zip",
        **kw,
    }


def at(day: int) -> datetime:
    return datetime(2026, 8, day, 4, 0, tzinfo=UTC)


def test_no_import_yet_takes_newest() -> None:
    assert one(select_run([run(20), run(19)], None))["run_id"] == "2026-08-20"


def test_newer_run_is_taken() -> None:
    assert one(select_run([run(20), run(19)], at(19)))["run_id"] == "2026-08-20"


def test_same_run_is_skipped() -> None:
    assert select_run([run(20), run(19)], at(20)) is None


def test_older_run_is_skipped() -> None:
    """Can happen if ATP republishes an older run — must not go backwards."""
    assert select_run([run(19)], at(20)) is None


def test_run_without_parquet_is_ignored() -> None:
    assert one(select_run([run(20, parquet_url=None), run(19)], at(18)))["run_id"] == "2026-08-19"


def test_no_usable_run_raises() -> None:
    with pytest.raises(RuntimeError, match="No ATP run"):
        select_run([run(20, parquet_url=None)], None)
