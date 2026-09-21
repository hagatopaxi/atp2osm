from typing import Any

from tests.conftest import ROOT, load_module

backfill = load_module(ROOT / "migrations" / "016_backfill_import_departements.py")
reconstruct = backfill.reconstruct


def change(
    dpt: int | str,
    changeset: int | None = None,
    tag: dict[str, str] | None = None,
    old_tag: dict[str, str] | None = None,
) -> dict[str, Any]:
    c: dict[str, Any] = {"departement_number": dpt, "tag": tag or {}, "old_tag": old_tag or {}}
    if changeset is not None:
        c["changeset"] = changeset
    return c


def test_success() -> None:
    changes = [change(6, 100), change(6, 100), change(94, 101)]
    children, uploaded = reconstruct(changes, [100, 101], "success", None)
    assert len(uploaded) == 3
    assert children == [
        {
            "departement_number": "06",
            "items_count": 2,
            "osm_changeset_id": 100,
            "status": "success",
            "comment": None,
        },
        {
            "departement_number": "94",
            "items_count": 1,
            "osm_changeset_id": 101,
            "status": "success",
            "comment": None,
        },
    ]


def test_partial_keeps_only_uploaded_pois() -> None:
    changes = [change(6, 100), change(94, 101)]
    children, uploaded = reconstruct(changes, [100], "partial_osm_api", "boom")
    assert uploaded == [changes[0]]
    assert [c["status"] for c in children] == ["success", "error_osm_api"]
    # the changeset was created, it is the upload that failed: keep it
    assert children[1]["osm_changeset_id"] == 101


def test_failed_department_without_changeset() -> None:
    children, _ = reconstruct([change(94)], [], "error_osm_api", "boom")
    assert children[0]["osm_changeset_id"] is None


def test_error_everywhere() -> None:
    changes = [change(6, 100), change(94)]
    children, _ = reconstruct(changes, [], "error_unknown", "boom")
    assert {c["status"] for c in children} == {"error_unknown"}


def test_old_log_without_changeset_list_falls_back_on_comment() -> None:
    changes = [change(6, 100), change(94, 101)]
    children, uploaded = reconstruct(
        changes, None, "partial_osm_api", "OSM API error for dept 94: HTTP 409"
    )
    assert uploaded == [changes[0]]
    assert [(c["departement_number"], c["status"]) for c in children] == [
        ("06", "success"),
        ("94", "error_osm_api"),
    ]


def test_dom_departement_number_is_not_padded_away() -> None:
    children, _ = reconstruct([change("971", 100)], [100], "success", None)
    assert children[0]["departement_number"] == "971"


def test_log_without_per_poi_changeset_maps_by_order() -> None:
    # some logs did not keep the "changeset" key on the POIs
    changes = [change(6), change(94), change(75)]
    children, uploaded = reconstruct(changes, [100, 101, 102], "success", None)
    assert [(c["departement_number"], c["osm_changeset_id"]) for c in children] == [
        ("06", 100),
        ("94", 101),
        ("75", 102),
    ]
    assert len(uploaded) == 3


def test_order_mapping_skips_the_department_named_in_the_comment() -> None:
    changes = [change(6), change(94), change(75)]
    children, _ = reconstruct(
        changes, [100, 101], "partial_osm_api", "OSM API error for dept 94: HTTP 409"
    )
    assert [(c["departement_number"], c["osm_changeset_id"], c["status"]) for c in children] == [
        ("06", 100, "success"),
        ("94", None, "error_osm_api"),
        ("75", 101, "success"),
    ]


def test_comment_overrides_a_department_listed_as_succeeded() -> None:
    # le commentaire nomme le département en échec : il tranche
    changes = [change(6, 100), change(94, 101)]
    children, uploaded = reconstruct(
        changes, [100, 101], "partial_unknown", "Unknown error for dept 94: boom"
    )
    assert [(c["departement_number"], c["status"]) for c in children] == [
        ("06", "success"),
        ("94", "error_unknown"),
    ]
    assert uploaded == [changes[0]]


def test_error_kind_is_read_per_department_in_the_comment() -> None:
    changes = [change(6), change(94), change(75)]
    children, _ = reconstruct(
        changes,
        [100],
        "partial",
        "OSM API error for dept 94: HTTP 409; Unknown error for dept 75: boom",
    )
    assert [(c["departement_number"], c["status"]) for c in children] == [
        ("06", "success"),
        ("94", "error_osm_api"),
        ("75", "error_unknown"),
    ]
