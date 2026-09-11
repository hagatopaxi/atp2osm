import pytest

from src.matching import (
    BATCH_MAX_SIZE,
    batch_scope,
    compose_batch,
    count_by_subdivision,
    pack_subdivisions,
    select_batch,
)
from src.upload import BulkUpload


def test_empty():
    assert pack_subdivisions({}, 200) == []


def test_everything_fits_in_one_batch():
    assert pack_subdivisions({"75": 50, "69": 30, "13": 20}, 200) == [["13", "69", "75"]]


def test_fills_the_remaining_room_with_the_biggest_that_fits():
    # 180 leaves 20: takes 20, not 15
    assert pack_subdivisions({"75": 180, "69": 20, "13": 15}, 200)[0] == ["69", "75"]


def test_no_batch_exceeds_max_and_all_subdivisions_are_used_once():
    counts = {str(i): (i * 37) % 190 + 1 for i in range(1, 96)}
    batches = pack_subdivisions(counts, 200)

    seen = [sub for batch in batches for sub in batch]
    assert sorted(seen) == sorted(counts)
    assert len(seen) == len(set(seen))
    for batch in batches:
        assert sum(counts[sub] for sub in batch) <= 200


def test_oversized_departement_gets_its_own_batch():
    # 75 is truncated to 200 by the caller, so it leaves no room for 69
    batches = pack_subdivisions({"75": 500, "69": 10}, 200)
    assert batches == [["75"], ["69"]]


def test_compose_batch_skips_blocked():
    counts = {"69": 120, "59": 90, "33": 60}
    assert compose_batch(counts, blocked={"69"}, max_size=200) == ["33", "59"]


def test_compose_batch_first_batch_only():
    counts = {"69": 150, "59": 150, "33": 150}
    assert compose_batch(counts, blocked=set(), max_size=200) == ["33"]


def test_compose_batch_all_blocked():
    assert compose_batch({"69": 10}, blocked={"69"}, max_size=200) == []


def _changes(**by_subdivision):
    return [{"subdivision_code": d} for d, n in by_subdivision.items() for _ in range(n)]


def test_select_batch_keeps_only_the_first_batch_and_describes_it():
    changes = select_batch(_changes(d69=6, d59=4, d23=3), set(), max_size=10)

    assert len(changes) == 10
    assert [c["subdivision_code"] for c in changes].count("d69") == 6
    assert batch_scope(changes) == [
        {"number": "d69", "name": "d69", "count": 6},
        {"number": "d59", "name": "d59", "count": 4},
    ]


def test_select_batch_never_cuts_a_departement_in_two():
    # 6 + 3 = 9: d59 (4) does not fit in the 4 slots left, so it moves whole to
    # the next batch rather than being cut, and the batch stays under the limit.
    changes = select_batch(_changes(d69=6, d59=4, d23=3), set(), max_size=9)

    assert len(changes) == 9
    assert [d["number"] for d in batch_scope(changes)] == ["d69", "d23"]


def test_select_batch_truncates_an_oversized_subdivision():
    changes = select_batch(_changes(d69=15), set(), max_size=10)

    assert len(changes) == 10
    assert batch_scope(changes) == [{"number": "d69", "name": "d69", "count": 10}]


def test_select_batch_returns_nothing_when_everything_is_blocked():
    assert select_batch(_changes(d69=3), blocked={"d69"}, max_size=10) == []


def test_select_batch_never_exceeds_max_size():
    # What upload_changes relies on: whatever the counts, a batch fits.
    for counts in ({"a": 300}, {"a": 60, "b": 60, "c": 60}, {"a": 99, "b": 1}):
        assert len(select_batch(_changes(**counts), set(), max_size=100)) <= 100


def test_bulk_upload_refuses_an_oversized_batch():
    # The chain guarantees it upstream; BulkUpload refuses anyway, before any
    # changeset is created.
    with pytest.raises(ValueError):
        BulkUpload([{"atp_brand": "x", "tag": {}}] * (BATCH_MAX_SIZE + 1), session=None)


def test_count_by_subdivision():
    changes = [{"subdivision_code": d} for d in ("69", "59", "69")]
    assert count_by_subdivision(changes) == {"69": 2, "59": 1}
