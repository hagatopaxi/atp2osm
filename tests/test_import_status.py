from src.utils import determine_import_status


def r(*statuses: str) -> list[dict[str, str]]:
    return [{"status": s} for s in statuses]


def test_all_success() -> None:
    assert determine_import_status(r("success", "success")) == "success"


def test_all_failed() -> None:
    assert determine_import_status(r("error_osm_api")) == "error"
    assert determine_import_status(r("error_osm_api", "error_unknown")) == "error"


def test_mixed_is_partial() -> None:
    # the error kind stays on the subdivision row, not here
    assert determine_import_status(r("success", "error_osm_api")) == "partial"
    assert determine_import_status(r("success", "error_unknown")) == "partial"


def test_no_changeset_at_all() -> None:
    assert determine_import_status([]) == "success"
