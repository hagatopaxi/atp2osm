from src.pipeline.atp import _parse_dated_log


def test_newest_commit_wins():
    log = (
        "2026-09-11T19:04:00Z\n\nlocations/spiders/a.py\n"
        "2026-06-30T16:00:27+01:00\n\nlocations/spiders/a.py\nlocations/spiders/b.py\n"
    )
    assert _parse_dated_log(log) == {
        "locations/spiders/a.py": "2026-09-11T19:04:00Z",
        "locations/spiders/b.py": "2026-06-30T16:00:27+01:00",
    }
