from src.pipeline.nsi import select_items


def _nsi(categories):
    return {"nsi": categories}


def _category(*items):
    return {"properties": {}, "items": list(items), "templates": []}


def _item(tags, location_set, **extra):
    return {"displayName": tags.get("brand", ""), "id": "x", "tags": tags,
            "locationSet": location_set, **extra}


def test_keeps_only_the_french_variant():
    """Eight McDonald's items are amenity=fast_food and differ only by
    locationSet: without the geographic filter, a Lyon McDonald's could be
    tagged in Japanese."""
    nsi = _nsi({"brands/amenity/fast_food": _category(
        _item({"brand": "McDonald's", "brand:wikidata": "Q38076", "amenity": "fast_food"},
              {"include": ["001"], "exclude": ["fr", "jp"]}),
        _item({"brand": "McDonald's", "brand:wikidata": "Q38076", "amenity": "fast_food"},
              {"include": ["fr"]}),
        _item({"brand": "マクドナルド", "brand:wikidata": "Q38076", "amenity": "fast_food"},
              {"include": ["jp"]}),
    )})

    rows = select_items(nsi)

    assert [row[1] for row in rows] == ["McDonald's"]


def test_worldwide_items_cover_france():
    nsi = _nsi({"brands/shop/clothes": _category(
        _item({"brand": "Zara", "brand:wikidata": "Q147662", "shop": "clothes"},
              {"include": ["001"]}),
        _item({"brand": "Uniqlo", "brand:wikidata": "Q26070", "shop": "clothes"},
              {"include": ["jp"]}),
    )})

    assert [row[1] for row in select_items(nsi)] == ["Zara"]


def test_same_qid_in_different_categories_all_survive():
    """A QID is not a unique key: E.Leclerc carries a dozen enseignes. Items
    telling themselves apart by category are all kept — the OSM object's own
    primary tag picks the right one later."""
    nsi = _nsi({
        "brands/shop/garden_centre": _category(
            _item({"brand": "E.Leclerc Jardi", "brand:wikidata": "Q1273376",
                   "shop": "garden_centre"}, {"include": ["fr"]})),
        "brands/amenity/fuel": _category(
            _item({"brand": "E.Leclerc", "brand:wikidata": "Q1273376",
                   "amenity": "fuel"}, {"include": ["fr"]})),
    })

    rows = select_items(nsi)

    assert sorted(row[3:5] for row in rows) == [
        ("amenity", "fuel"), ("shop", "garden_centre"),
    ]


def test_same_qid_and_category_agreeing_on_tags_are_kept():
    """Three E.Leclerc are shop=supermarket and carry different names, but name
    is not written: what they would write is identical, so there is nothing to
    disambiguate. All three labels stay available for QID recovery."""
    nsi = _nsi({"brands/shop/supermarket": _category(
        _item({"brand": "E.Leclerc", "brand:wikidata": "Q1273376", "shop": "supermarket"},
              {"include": ["fr"]}),
        _item({"brand": "E.Leclerc Express", "brand:wikidata": "Q1273376", "shop": "supermarket"},
              {"include": ["fr"]}),
    )})

    rows = select_items(nsi)

    assert [row[1] for row in rows] == ["E.Leclerc", "E.Leclerc Express"]
    assert {row[5]["brand:wikidata"] for row in rows} == {"Q1273376"}


def test_same_qid_and_category_disagreeing_on_tags_cancel_each_other():
    """Intermarché and Intermarché Drive are both shop=supermarket, and only
    one is drive_through=only. Nothing tells them apart, so neither applies."""
    nsi = _nsi({"brands/shop/supermarket": _category(
        _item({"brand": "Intermarché", "brand:wikidata": "Q3153200", "shop": "supermarket"},
              {"include": ["001"]}),
        _item({"brand": "Intermarché Drive", "brand:wikidata": "Q3153200",
               "shop": "supermarket", "drive_through": "only"}, {"include": ["fx"]}),
        _item({"brand": "Lidl", "brand:wikidata": "Q151954", "shop": "supermarket"},
              {"include": ["fr"]}),
    )})

    assert [row[1] for row in select_items(nsi)] == ["Lidl"]


def test_only_writable_tags_are_kept():
    """operator disagrees with the ground 13% of the time (NSI says
    'Société Générale' where OSM says 'SG'), operator:wikidata never does."""
    nsi = _nsi({"brands/amenity/atm": _category(
        _item({"amenity": "atm", "brand": "Crédit Agricole", "brand:wikidata": "Q590952",
               "name": "Crédit Agricole", "operator": "Crédit Agricole",
               "operator:wikidata": "Q590952"}, {"include": ["fr"]}),
    )})

    (row,) = select_items(nsi)

    assert row[5] == {
        "amenity": "atm",
        "brand:wikidata": "Q590952",
        "operator:wikidata": "Q590952",
    }
    # brand and name stay out of the written tags, but remain available as
    # lookup labels for the QID recovery in mv_places.
    assert row[1] == "Crédit Agricole"
    assert row[2] == "Crédit Agricole"


def test_ignores_transit_and_items_without_qid():
    nsi = _nsi({
        "transit/route/tram": _category(
            _item({"brand": "T1", "brand:wikidata": "Q42", "route": "tram"},
                  {"include": ["fr"]})),
        "brands/shop/bakery": _category(
            _item({"brand": "No QID", "shop": "bakery"}, {"include": ["fr"]})),
    })

    assert select_items(nsi) == []


def test_ignores_categories_generic_lua_drops():
    """An advertising=totem object never reaches points/polygons, so its NSI
    row could never match. Worse, it would make its brand look multi-entry and
    cost it the single-entry shortcut."""
    nsi = _nsi({
        "brands/advertising/totem": _category(
            _item({"brand": "Intermarché", "brand:wikidata": "Q3153200",
                   "advertising": "totem"}, {"include": ["fr"]})),
        "brands/man_made/charge_point": _category(
            _item({"brand": "TotalEnergies", "brand:wikidata": "Q154037",
                   "man_made": "charge_point"}, {"include": ["fr"]})),
        "brands/shop/supermarket": _category(
            _item({"brand": "Intermarché", "brand:wikidata": "Q3153200",
                   "shop": "supermarket"}, {"include": ["fr"]})),
    })

    rows = select_items(nsi)

    assert [row[3:5] for row in rows] == [("shop", "supermarket")]


def test_metropolitan_france_code_counts_as_french():
    """NSI uses fx (metropolitan France) for over a thousand items — this
    project's exact scope. Reading fr only dropped Manège à Bijoux and Une
    Heure Pour Soi, both scoped {'include': ['fx']}."""
    nsi = _nsi({"brands/shop/jewelry": _category(
        _item({"brand": "Manège à Bijoux", "brand:wikidata": "Q1273376",
               "shop": "jewelry"}, {"include": ["fx"]}),
    )})

    assert [row[1] for row in select_items(nsi)] == ["Manège à Bijoux"]


def test_overseas_only_brands_are_kept():
    """The pipeline imports Guyane, Nouvelle-Calédonie and the rest from
    Geofabrik, so a brand scoped to them alone still has objects to match."""
    nsi = _nsi({"brands/amenity/fuel": _category(
        _item({"brand": "Sol", "brand:wikidata": "Q3488375", "amenity": "fuel"},
              {"include": ["029", "gf", "gy", "sr"]}),
    )})

    assert [row[1] for row in select_items(nsi)] == ["Sol"]


def test_stamp_changes_with_the_deployed_revision(monkeypatch):
    """A new revision is a new import, whatever NSI published.

    Half of what lands in nsi_brands comes from this module — which tags are
    writable, which trees are kept. Editing them leaves the published version
    untouched, so a guard on that version alone would hold the change back
    until NSI publishes again.
    """
    from src.pipeline import nsi

    before = nsi._stamp("8.0.20260729")
    monkeypatch.setattr(nsi, "app_version", lambda: "Gamma-decafe")
    assert nsi._stamp("8.0.20260729") != before


def test_stamp_carries_the_published_version(monkeypatch):
    from src.pipeline import nsi

    assert nsi._stamp("8.0.20260729").startswith("8.0.20260729+")


def test_a_containing_code_counts_on_both_sides():
    """An item scoped to the world applies here; the world minus Europe does not."""
    from src.pipeline.nsi import _is_country

    assert _is_country({"include": ["001"]})
    assert _is_country({"include": ["150"], "exclude": ["de"]})
    assert not _is_country({"include": ["001"], "exclude": ["150"]})
    assert not _is_country({"include": ["001"], "exclude": ["fr"]})
    assert not _is_country({"include": ["de", "be"]})
