"""The reads wave 2 makes on the OSM API, and what a failed one means.

The API is staged one response at a time: what the tests control is the
request that goes out and the verdict that comes back — never the network,
which the suite refuses (see conftest).
"""

from datetime import UTC, datetime
from typing import Any

import pytest
import requests

from src import osm_history
from src.config import get_settings
from src.osm_history import (
    OsmApiUnavailableError,
    Version,
    is_bot,
    protect_recent_edits,
    versions,
)
from tests.conftest import make_change, one

pytestmark = pytest.mark.usefixtures("guard_on")


class _Response:
    def __init__(
        self, status: int = 200, payload: dict[str, Any] | None = None, body: str | None = None
    ) -> None:
        self.status_code = status
        self._payload = payload
        self._body = body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict[str, Any] | None:
        if self._body is not None:
            raise ValueError("not JSON")
        return self._payload


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    """The next answers of the API, and the requests it received."""
    state: dict[str, list[Any]] = {"answers": [], "requests": []}

    def get(
        url: str, headers: dict[str, str] | None = None, timeout: tuple[float, float] | None = None
    ) -> _Response:
        state["requests"].append({"url": url, "headers": headers, "timeout": timeout})
        answer = state["answers"].pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(osm_history.requests, "get", get)
    is_bot.cache_clear()
    return state


def version(
    number: int, when: datetime | None = None, tags: dict[str, str] | None = None
) -> Version:
    return {
        "version": number,
        "timestamp": (when or datetime(2020, 1, 1, tzinfo=UTC)).isoformat(),
        "changeset": 100 + number,
        "tags": tags,
    }


# --- The request ------------------------------------------------------------


def test_a_read_is_identified_and_bounded(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={"elements": []})]
    versions("node", 1)
    (sent,) = api["requests"]
    assert sent["url"] == f"{get_settings().api_url}/api/0.6/node/1/history.json"
    assert sent["headers"]["User-Agent"].startswith("atp2osm/")
    assert sent["timeout"] == osm_history.TIMEOUT


def test_a_way_and_a_relation_are_read_under_their_own_type(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={}), _Response(payload={})]
    versions("way", 2)
    versions("relation", 3)
    assert [r["url"].rsplit("/0.6/", 1)[1] for r in api["requests"]] == [
        "way/2/history.json",
        "relation/3/history.json",
    ]


# --- versions() --------------------------------------------------------------


def test_versions_come_back_oldest_first_whatever_the_api_order(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={"elements": [version(3), version(1), version(2)]})]
    assert [v["version"] for v in versions("node", 1)] == [1, 2, 3]


@pytest.mark.parametrize("payload", [{}, {"elements": None}, {"elements": []}])
def test_an_answer_without_versions_is_an_empty_history(
    api: dict[str, list[Any]], payload: dict[str, Any]
) -> None:
    api["answers"] = [_Response(payload=payload)]
    assert versions("node", 1) == []


# --- is_bot() ----------------------------------------------------------------


def test_a_changeset_declaring_itself_a_bot_is_one(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={"elements": [{"tags": {"bot": "yes"}}]})]
    assert is_bot(1) is True


@pytest.mark.parametrize(
    "element",
    [
        {"tags": {"created_by": "JOSM"}},  # a tool, not a nature
        {"tags": {"bot": "no"}},
        {"tags": {}},
        {"tags": None},
        {},
    ],
)
def test_a_changeset_not_declaring_itself_is_a_human(
    api: dict[str, list[Any]], element: dict[str, Any]
) -> None:
    """The doubt benefits what is there."""
    api["answers"] = [_Response(payload={"elements": [element]})]
    assert is_bot(1) is False


def test_an_unknown_changeset_is_a_human(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={"elements": []})]
    assert is_bot(1) is False


def test_a_changeset_is_asked_once_per_process(api: dict[str, list[Any]]) -> None:
    api["answers"] = [_Response(payload={"elements": [{"tags": {"bot": "yes"}}]})]
    assert is_bot(7) is True
    assert is_bot(7) is True
    assert len(api["requests"]) == 1


# --- Failures ----------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        requests.ConnectionError("dns"),
        requests.Timeout("read"),
        _Response(status=500),
        _Response(status=503),
        _Response(status=404),
        _Response(body="<html>maintenance</html>"),
    ],
    ids=["connection", "timeout", "500", "503", "404", "not-json"],
)
def test_a_read_that_fails_is_an_outage_not_a_value(
    api: dict[str, list[Any]], failure: Exception | _Response
) -> None:
    api["answers"] = [failure]
    with pytest.raises(OsmApiUnavailableError):
        versions("node", 1)


def test_a_failed_bot_verdict_is_not_cached(api: dict[str, list[Any]]) -> None:
    api["answers"] = [
        _Response(status=502),
        _Response(payload={"elements": [{"tags": {"bot": "yes"}}]}),
    ]
    with pytest.raises(OsmApiUnavailableError):
        is_bot(1)
    assert is_bot(1) is True


def test_the_protection_surfaces_the_outage_instead_of_keeping_everything(
    api: dict[str, list[Any]],
) -> None:
    """A batch whose every value went undated must not come out empty: the
    route would then close the brand as integrated for a whole cooldown.
    """
    api["answers"] = [requests.ConnectionError("down")]
    recent = datetime.now(UTC).isoformat()
    change = make_change(
        tag={"phone": "+33 1 00 00 00 00"},
        old_tag={"phone": "+33 1 11 11 11 11"},
        osm_timestamp=recent,
    )
    with pytest.raises(OsmApiUnavailableError):
        protect_recent_edits([change])


def test_the_history_is_read_before_any_changeset(api: dict[str, list[Any]]) -> None:
    """One outage stops at the first read: no verdict is asked on a history
    that could not be read.
    """
    api["answers"] = [_Response(status=500)]
    recent = datetime.now(UTC).isoformat()
    change = make_change(tag={"phone": "a"}, old_tag={"phone": "b"}, osm_timestamp=recent)
    with pytest.raises(OsmApiUnavailableError):
        protect_recent_edits([change])
    assert len(api["requests"]) == 1


# --- parse_timestamp() ----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""])
def test_no_timestamp_is_no_date(value: str | None) -> None:
    assert osm_history.parse_timestamp(value) is None


def test_an_osm_timestamp_is_read_as_utc() -> None:
    when = osm_history.parse_timestamp("2026-03-01T10:00:00Z")
    assert when == datetime(2026, 3, 1, 10, tzinfo=UTC)


def test_a_naive_datetime_is_taken_as_utc() -> None:
    when = one(osm_history.parse_timestamp(datetime(2026, 3, 1, 10)))  # noqa: DTZ001 — the naive case
    assert when.tzinfo is UTC


def test_an_aware_datetime_is_kept() -> None:
    given = datetime(2026, 3, 1, 10, tzinfo=UTC)
    assert osm_history.parse_timestamp(given) is given


def test_a_version_without_tags_is_an_absence() -> None:
    """A deleted version carries no tags: the value is absent there, which
    breaks the run of a value recreated afterwards.
    """
    old = datetime(2020, 1, 1, tzinfo=UTC)
    history = [
        version(1, old, {"phone": "a"}),
        {"version": 2, "timestamp": old.isoformat(), "changeset": 102},  # deleted
        version(3, old, {"phone": "a"}),
    ]
    _, changeset = osm_history.value_set_at(history, "phone")
    assert changeset == 103
