from src.pipeline.atp import is_relevant_spider


def test_keeps_french_and_suffixless_spiders() -> None:
    assert is_relevant_spider("output/carrefour_fr.geojson")
    assert is_relevant_spider("ikea.geojson")
    assert is_relevant_spider("output/mcdonalds_eu.geojson")
    assert is_relevant_spider("output/some_brand.geojson")


def test_drops_foreign_spiders() -> None:
    assert not is_relevant_spider("output/aldi_de.geojson")
    assert not is_relevant_spider("starbucks_us.geojson")
    assert not is_relevant_spider("output/tesco_GB.geojson")


def test_drops_bulk_address_datasets() -> None:
    assert not is_relevant_spider("output/au_vic_addresses.geojson")
    assert not is_relevant_spider("nz_addresses.geojson")
    assert not is_relevant_spider("output/best_vlg_addresses_be.geojson")


def test_keeps_brands_whose_name_starts_like_a_country_code() -> None:
    # `la`, `au`, `as` are country codes, but here they are the brand itself.
    assert is_relevant_spider("output/la_halle_fr.geojson")
    assert is_relevant_spider("la_vie_claire_fr.geojson")
    assert is_relevant_spider("output/au_vieux_campeur.geojson")
    assert is_relevant_spider("as_24_fr.geojson")
