from typing import Any

from src.matching import Change, apply_on_node
from tests.conftest import one


def match(tags: dict[str, str], **atp: Any) -> Change | None:  # noqa: ANN401 — a row's columns
    base: dict[str, Any] = {
        "osm_id": 1,
        "version": 1,
        "node_type": "node",
        "tags": tags,
        "lon": 1,
        "lat": 2,
        "brand": "Babylone",
        "id": "atp-1",
        "source_uri": "https://babylone.fr",
        "source_type": "spider",
        "postcode": "75001",
        "subdivision_code": "75",
        "subdivision_name": "Paris",
        "atp_opening_hours": None,
        "atp_website": None,
        "atp_phone": None,
        "atp_email": None,
    }
    base.update(atp)
    return apply_on_node(base)


def test_apply_on_node_default() -> None:
    res = one(match({"addr:city": "Babylone"}, atp_email="contact@babylone.fr"))
    assert res["id"] == 1
    assert res["tag"] == {
        "addr:city": "Babylone",
        "email": "contact@babylone.fr",
    }


def test_apply_on_node_keep_contact_phone() -> None:
    res = one(
        match(
            {"contact:phone": "0622334455"},
            atp_phone="+33622334455",
            atp_email="contact@babylone.fr",
        )
    )
    assert res["tag"] == {
        "contact:phone": "0622334455",
        "email": "contact@babylone.fr",
    }


def test_apply_on_node_keep_contact_email() -> None:
    res = match({"contact:email": "contact@babylone.fr"}, atp_email="contact@babylone.fr")
    assert res is None


def test_apply_on_node_relation_id_is_negated() -> None:
    # osm2pgsql stores relations with a negative area_id
    res = one(match({}, osm_id=-42, node_type="relation", atp_opening_hours="Mo-Fr 09:00-18:00"))
    assert res["id"] == 42
    assert res["node_type"] == "relation"


def test_apply_on_node_not_brand_wikidata_blocks_matching_value() -> None:
    # not:brand:wikidata=Q123 should prevent brand:wikidata=Q123 from being applied
    res = one(
        match(
            {"not:brand:wikidata": "Q123"}, nsi_tags={"brand:wikidata": "Q123", "shop": "clothes"}
        )
    )
    assert res["tag"] == {"not:brand:wikidata": "Q123", "shop": "clothes"}
    assert "brand:wikidata" not in res["tag"]


def test_apply_on_node_not_brand_wikidata_allows_different_value() -> None:
    # not:brand:wikidata=Q123 should NOT prevent brand:wikidata=Q456 from being applied
    res = one(
        match(
            {"not:brand:wikidata": "Q123"}, nsi_tags={"brand:wikidata": "Q456", "shop": "clothes"}
        )
    )
    assert res["tag"] == {"not:brand:wikidata": "Q123", "brand:wikidata": "Q456", "shop": "clothes"}


def test_apply_on_node_not_tag_blocks_any_tag() -> None:
    # not:shop=fuel should prevent shop=fuel from being applied
    res = one(match({"not:shop": "fuel"}, nsi_tags={"shop": "fuel", "amenity": "fuel"}))
    assert res["tag"] == {"not:shop": "fuel", "amenity": "fuel"}
    assert "shop" not in res["tag"]


def test_apply_on_node_rewrites_08_in_national_notation() -> None:
    # 08 numbers are not reachable from abroad: OSM wants them written the
    # national way, unlike every other number ATP hands over.
    res = one(match({}, atp_phone="+33 8 92 70 12 34"))
    assert res["tag"]["phone"] == "08 92 70 12 34"


def test_apply_on_node_leaves_other_numbers_alone() -> None:
    res = one(match({}, atp_phone="+33 1 23 45 67 89"))
    assert res["tag"]["phone"] == "+33 1 23 45 67 89"
