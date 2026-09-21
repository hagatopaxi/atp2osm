from src.matching import BATCH_SAMPLE_SIZE, Change, changed_tags, sample_for_review
from tests.conftest import make_change


def change(**tags: str) -> Change:
    return make_change(tag=tags)


def covered(sample: list[Change]) -> set[str]:
    return set[str]().union(*(changed_tags(c) for c in sample))


def test_every_changed_tag_appears_in_the_sample() -> None:
    changes = [change(**{t: "x"}) for t in ("website", "phone", "opening_hours", "email")]
    changes += [change(website="x") for _ in range(20)]

    sample = sample_for_review(changes)

    assert covered(sample) == {"website", "phone", "opening_hours", "email"}


def test_tops_up_to_the_minimum_when_few_tags() -> None:
    changes = [change(website="x") for _ in range(10)]

    assert len(sample_for_review(changes)) == BATCH_SAMPLE_SIZE


def test_small_batch_reviewed_in_full() -> None:
    changes = [change(website="x"), change(phone="x")]

    assert sample_for_review(changes) == changes


def test_unchanged_pois_never_break_the_sample() -> None:
    changes = [make_change(tag={"website": "x"}, old_tag={"website": "x"})]

    assert sample_for_review(changes) == changes
