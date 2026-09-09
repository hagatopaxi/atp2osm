"""Wave 2 never overwrites a value a human posted recently.

The API is never called: the version list and the bot verdict are handed over
directly, which is what the two functions under test read.
"""

from datetime import datetime, timedelta, timezone

import pytest

import src.osm_history as osm_history
from src.osm_history import protect_recent_edits, value_set_at

NOW = datetime.now(timezone.utc)
OLD = NOW - timedelta(weeks=52)
RECENT = NOW - timedelta(days=3)


def version(number, value, when, changeset):
    return {
        "version": number,
        "timestamp": when.isoformat(),
        "changeset": changeset,
        "tags": {} if value is None else {"phone": value},
    }


def test_the_oldest_consecutive_version_carrying_the_value_is_the_one_that_posted_it():
    history = [
        version(1, "0100000000", OLD, 10),
        version(2, "0123456789", OLD, 11),
        version(3, "0123456789", RECENT, 12),  # a rewrite, not a posting
    ]
    when, changeset = value_set_at(history, "phone")
    assert (when, changeset) == (osm_history._parse(OLD.isoformat()), 11)


def test_a_value_recreated_after_a_deletion_was_posted_again():
    """Absence is a value like any other: the deletion breaks the run."""
    history = [
        version(1, "0123456789", OLD, 10),
        version(2, None, OLD, 11),
        version(3, "0123456789", RECENT, 12),
    ]
    _, changeset = value_set_at(history, "phone")
    assert changeset == 12


def test_a_single_version_object_answers_v1():
    _, changeset = value_set_at([version(1, "0123456789", OLD, 10)], "phone")
    assert changeset == 10


@pytest.fixture
def api(monkeypatch):
    """The two API reads, staged."""
    state = {"history": [], "bot": False, "calls": 0}

    def versions(node_type, osm_id):
        state["calls"] += 1
        return state["history"]

    monkeypatch.setattr(osm_history, "versions", versions)
    monkeypatch.setattr(osm_history, "is_bot", lambda changeset: state["bot"])
    return state


def change(tag, old_tag, osm_timestamp):
    return {
        "id": 1,
        "node_type": "node",
        "tag": dict(tag),
        "old_tag": dict(old_tag),
        "osm_timestamp": osm_timestamp.isoformat(),
    }


def test_a_recent_human_value_is_kept_and_the_poi_drops_out(api):
    api["history"] = [version(1, "0100000000", RECENT, 12)]
    kept = protect_recent_edits(
        [change({"phone": "0123456789"}, {"phone": "0100000000"}, RECENT)]
    )
    assert kept == []


def test_a_recent_bot_value_is_replaced(api):
    api["history"] = [version(1, "0100000000", RECENT, 12)]
    api["bot"] = True
    kept = protect_recent_edits(
        [change({"phone": "0123456789"}, {"phone": "0100000000"}, RECENT)]
    )
    assert kept[0]["tag"] == {"phone": "0123456789"}


def test_an_old_object_costs_no_request(api):
    kept = protect_recent_edits(
        [change({"phone": "0123456789"}, {"phone": "0100000000"}, OLD)]
    )
    assert api["calls"] == 0
    assert kept[0]["tag"] == {"phone": "0123456789"}


def test_an_addition_is_never_protected(api):
    """Wave 1 overwrites nothing, so it asks nothing."""
    kept = protect_recent_edits([change({"phone": "0123456789"}, {}, RECENT)])
    assert api["calls"] == 0
    assert kept[0]["tag"] == {"phone": "0123456789"}


def test_the_protection_is_per_tag_not_per_object(api):
    """A POI whose name was fixed yesterday still takes a website."""
    api["history"] = [
        {
            "version": 1,
            "timestamp": OLD.isoformat(),
            "changeset": 10,
            "tags": {"name": "Babylon", "website": "https://old.example"},
        },
        {
            "version": 2,
            "timestamp": RECENT.isoformat(),
            "changeset": 12,
            "tags": {"name": "Babylone", "website": "https://old.example"},
        },
    ]
    kept = protect_recent_edits([
        change(
            {"name": "Babylone Paris", "website": "https://babylone.fr"},
            {"name": "Babylone", "website": "https://old.example"},
            RECENT,
        )
    ])
    assert kept[0]["tag"] == {
        "name": "Babylone",                  # posted by a human three days ago
        "website": "https://babylone.fr",    # untouched for a year
    }
