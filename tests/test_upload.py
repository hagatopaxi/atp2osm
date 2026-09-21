"""What `BulkUpload.upload` sends to OSM, and what it does when a send fails.

The uploads go through `FakeOsmApi`, the same stand-in development runs on,
so the code path exercised here is the one production takes — there is no
longer a branch that only fires outside the tests. Only the OSC writing is
turned off: reading the generated files is a development need, and a test
that wrote them would litter the checkout.
"""

import json
from pathlib import Path
from typing import Any, Never

import pytest
from osmapi.errors import ApiError
from requests_oauthlib import OAuth2Session

from src.matching import Change
from src.upload import BulkUpload, Element, FakeOsmApi
from tests.conftest import make_change, one


@pytest.fixture(autouse=True)
def no_osc(monkeypatch: pytest.MonkeyPatch) -> None:
    def nothing(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(BulkUpload, "_write_osc", nothing)


def change(
    osm_id: int,
    node_type: str = "node",
    sub: str = "75",
    name: str = "Paris",
    members: list[Any] | None = None,
) -> Change:
    return make_change(
        id=osm_id,
        version=3,
        node_type=node_type,
        tag={"brand": "Babylone", "brand:wikidata": "Q123"},
        members=members or [],
        subdivision_code=sub,
        subdivision_name=name,
    )


def bulk_upload(changes: list[Change]) -> BulkUpload:
    return BulkUpload(changes, session=OAuth2Session())


def upload(changes: list[Change]) -> tuple[BulkUpload, list[tuple[str, str]]]:
    bulk = bulk_upload(changes)
    return bulk, bulk.upload()


def fake(bulk: BulkUpload) -> FakeOsmApi:
    """The API the upload drove — in development, always the fake."""
    assert isinstance(bulk.api, FakeOsmApi)
    return bulk.api


def calls(bulk: BulkUpload, kind: str) -> list[tuple[str, Any]]:
    return [c for c in fake(bulk).calls if c[0] == kind]


def test_one_changeset_per_subdivision() -> None:
    bulk, errors = upload([change(1), change(2, sub="33", name="Gironde")])

    assert errors == []
    assert len(calls(bulk, "changeset_create")) == 2
    assert len(bulk.changesets) == 2
    assert [(r["subdivision_code"], r["items_count"], r["status"]) for r in bulk.results] == [
        ("75", 1, "success"),
        ("33", 1, "success"),
    ]


def test_changeset_tags_name_the_subdivision_and_the_brand() -> None:
    bulk, _ = upload([change(1)])

    _, tags = calls(bulk, "changeset_create")[0]
    assert "Paris" in tags["comment"]
    assert "Babylone" in tags["comment"]
    assert tags["bot"] == "yes"
    assert tags["created_by"] == "atp2osm"


def test_nodes_go_in_one_changeset_upload() -> None:
    bulk, _ = upload([change(1), change(2)])

    ((_, payload),) = calls(bulk, "changeset_upload")
    assert payload[0]["type"] == "node"
    assert payload[0]["action"] == "modify"
    assert [n["id"] for n in payload[0]["data"]] == [1, 2]
    # The changeset the POI was sent in is written back on it.
    assert {n["changeset"] for n in payload[0]["data"]} == {1}


def test_a_way_is_updated_on_its_own() -> None:
    bulk, _ = upload([change(7, node_type="way", members=[10, 11])])

    ((_, data),) = calls(bulk, "way_update")
    assert data["id"] == 7
    assert data["nd"] == [10, 11]
    assert data["version"] == 3
    assert calls(bulk, "changeset_upload") == []


def test_a_relation_member_types_are_spelled_out() -> None:
    # osm2pgsql stores member types as a single letter; the API wants the word.
    members = [
        {"type": "n", "ref": 1, "role": ""},
        {"type": "w", "ref": 2, "role": "outer"},
        {"type": "r", "ref": 3, "role": "inner"},
    ]
    bulk, _ = upload([change(9, node_type="relation", members=members)])

    ((_, data),) = calls(bulk, "relation_update")
    assert [m["type"] for m in data["member"]] == ["node", "way", "relation"]
    assert [m["role"] for m in data["member"]] == ["", "outer", "inner"]


def test_every_changeset_is_closed() -> None:
    bulk, _ = upload([change(1), change(2, sub="33", name="Gironde")])

    assert len(calls(bulk, "changeset_close")) == 2
    assert fake(bulk).open_changeset == 0


def test_a_failing_subdivision_does_not_take_the_next_one_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The reason the `finally` exists: osmapi keeps the changeset open after an
    # error, and every later subdivision then fails with "Changeset already
    # opened". One bad subdivision must cost exactly one subdivision.
    bulk = bulk_upload([change(1), change(2, sub="33", name="Gironde")])
    real_upload = bulk.api.changeset_upload

    def fail_on_paris(changes: list[Element]) -> list[Element]:
        if changes[0]["data"][0]["id"] == 1:
            raise ApiError(400, "Bad Request", b"conflict")
        return real_upload(changes)

    monkeypatch.setattr(bulk.api, "changeset_upload", fail_on_paris)
    errors = bulk.upload()

    assert [kind for kind, _ in errors] == ["osm_api"]
    assert [r["status"] for r in bulk.results] == ["error_osm_api", "success"]
    assert [c["id"] for c in bulk.uploaded_changes] == [2]
    assert len(bulk.changesets) == 1


def test_a_changeset_that_could_not_be_created_is_recorded_without_an_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk = bulk_upload([change(1)])

    def refuse(_tags: dict[str, str]) -> Never:
        raise ApiError(509, "Bandwidth Limit Exceeded", b"")

    monkeypatch.setattr(bulk.api, "changeset_create", refuse)
    errors = bulk.upload()

    assert [kind for kind, _ in errors] == ["osm_api"]
    assert bulk.results[0]["osm_changeset_id"] is None
    assert bulk.uploaded_changes == []


def test_an_unexpected_error_is_told_apart_from_an_api_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bulk = bulk_upload([change(1)])

    def boom(_changes: list[Element]) -> Never:
        raise KeyError("members")

    monkeypatch.setattr(bulk.api, "changeset_upload", boom)
    errors = bulk.upload()

    assert [kind for kind, _ in errors] == ["unknown"]
    assert bulk.results[0]["status"] == "error_unknown"


def test_nothing_to_upload_sends_nothing() -> None:
    bulk, errors = upload([])
    assert errors == []
    assert fake(bulk).calls == []


def test_a_changeset_left_open_by_a_failure_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    bulk = bulk_upload([change(1), change(2, sub="33", name="Gironde")])

    def fail(_changes: list[Element]) -> Never:
        raise ApiError(400, "Bad Request", b"conflict")

    monkeypatch.setattr(bulk.api, "changeset_upload", fail)
    bulk.upload()

    # One close per changeset opened, failed or not: nothing stays open on OSM.
    assert len(calls(bulk, "changeset_close")) == 2
    assert fake(bulk).open_changeset == 0


def test_a_close_that_fails_too_does_not_block_the_next_subdivision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Osmapi refuses to open a changeset while it believes one is open."""
    bulk = bulk_upload([change(1), change(2, sub="33", name="Gironde")])
    real_upload, real_close = bulk.api.changeset_upload, bulk.api.changeset_close

    def fail_on_paris(changes: list[Element]) -> list[Element]:
        if changes[0]["data"][0]["id"] == 1:
            raise ApiError(400, "Bad Request", b"conflict")
        return real_upload(changes)

    def close_fails_once() -> int:
        if fake(bulk).open_changeset == 1:
            raise ApiError(500, "Internal Server Error", b"")
        return real_close()

    monkeypatch.setattr(bulk.api, "changeset_upload", fail_on_paris)
    monkeypatch.setattr(bulk.api, "changeset_close", close_fails_once)
    bulk.upload()

    assert [r["status"] for r in bulk.results] == ["error_osm_api", "success"]


def test_the_run_is_logged_with_its_changes_and_changesets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    bulk, _ = upload([change(1)])
    path = one(bulk.save_log_file())

    log = json.loads(path.read_text())
    assert [c["id"] for c in log["changes"]] == [1]
    assert log["changesets"] == bulk.changesets
    assert path.parent.name == bulk.brand_wikidata


def test_an_empty_run_writes_no_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    bulk, _ = upload([])
    assert bulk.save_log_file() is None
    assert not (tmp_path / "logs").exists()
