from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Never

import pytest

import src.config
from src import utils


def _stub_api(monkeypatch: pytest.MonkeyPatch, calls: list[list[int]]) -> None:
    """Replace the network call and record the requested ids."""

    class Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, list[dict[str, dict[str, int | str]]]]:
            return {
                "users": [{"user": {"id": uid, "display_name": f"u{uid}"}} for uid in calls[-1]]
            }

    def fake_get(url: str, **_kwargs: object) -> Resp:
        ids = [int(i) for i in url.split("users=")[1].split(",")]
        calls.append(ids)
        return Resp()

    monkeypatch.setattr(utils.requests, "get", fake_get)
    monkeypatch.setattr(
        src.config,
        "get_settings",
        lambda: SimpleNamespace(api_url="http://osm.test", app_version="test"),
    )


def test_cache_avoids_refetch_then_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    utils.osm_user_cache.clear()
    calls: list[list[int]] = []
    _stub_api(monkeypatch, calls)

    assert utils.fetch_osm_users([1, 2]) == {1: "u1", 2: "u2"}
    assert calls == [[1, 2]]

    # Second call: everything is cached, no network call.
    assert utils.fetch_osm_users([1, 2]) == {1: "u1", 2: "u2"}
    assert calls == [[1, 2]]

    # Only the unknown id goes to the network.
    assert utils.fetch_osm_users([1, 3]) == {1: "u1", 3: "u3"}
    assert calls[-1] == [3]

    # Once expired, we refetch.
    utils.osm_user_cache[1] = ("u1", datetime.now(UTC) - timedelta(seconds=1))
    utils.fetch_osm_users([1])
    assert calls[-1] == [1]


def test_api_failure_serves_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    utils.osm_user_cache.clear()
    calls: list[list[int]] = []
    _stub_api(monkeypatch, calls)
    utils.fetch_osm_users([1])

    def boom(*_a: object, **_kw: object) -> Never:
        raise RuntimeError("API down")

    monkeypatch.setattr(utils.requests, "get", boom)
    assert utils.fetch_osm_users([1, 2]) == {1: "u1"}
