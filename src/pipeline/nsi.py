"""NSI (name-suggestion-index) datasource.

NSI describes brands, never POIs: it maps a brand:wikidata to a canonical set
of tags. ATP describes POIs. The two sources barely overlap, which is what
makes them complementary — see specs/02_source-nsi.md.
"""

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone

import requests

from src.config import get_country
from src.pipeline._version import app_version
from src.pipeline.errors import unavailable_if_unreachable
from src.pipeline._db import (
    connect,
    last_import_comment,
    record_import,
    start_import,
)
from src.pipeline.constants import (
    NSI_CDN_URL,
    NSI_DIR,
    NSI_PATH,
    NSI_REGISTRY_URL,
)
from src.utils import download_large_file

logger = logging.getLogger(__name__)


# The only tags ever written to OSM live in the country configuration
# (`nsi_writable_tags`); every other NSI key is dropped on import, which makes
# that list the single place where the scope is defined.
#
# It is not a universal constant, and that is why it is configuration rather
# than code: it is produced by scripts/calibrate_nsi_tags.py, which measures for
# each NSI tag how often it agrees with what the country's own OSM objects of
# the same brand already carry. The French list keeps what reaches at least 98%
# agreement over at least 50 objects — rerun the script to re-establish it for
# another country, never copy it across.
#
# Deliberately out of the French one, all three well under the threshold and for
# the same reason: name (86.2%), brand (97.0%) and operator (96.7%). Their
# disagreements are systematic, not noise — NSI carries the national umbrella
# where the ground carries the real, more precise entity (Crédit Mutuel de
# Bretagne, Banque Populaire Alsace Lorraine Champagne), and NSI leads or trails
# rebrandings (SG / Société Générale, TotalEnergies / Total). Writing them would
# destroy better information than ours.
#
# The primary keys (shop, amenity, office, tourism, leisure, healthcare, craft)
# measure 100% by construction: nsi_match already drops a primary key the object
# disagrees with, see migration 021.

# NSI groups items in four trees. transit (routes, networks) and flags describe
# things generic.lua already drops from the OSM side via is_definitely_not_a_place,
# so they could never match anything here.
_TREES = ("brands", "operators")

# Categories whose objects generic.lua drops before they ever reach points /
# polygons, so no OSM object can ever carry them. Keeping them would not just
# be dead weight: an unreachable advertising=totem row makes its brand look
# multi-entry, which downgrades it from "applies unconditionally" to "needs a
# matching primary tag". 71 rows, and 40 brands recover their single-entry
# shortcut.
_UNREACHABLE_KEYS = frozenset({
    "advertising", "aerialway", "aeroway", "barrier", "bicycle_road",
    "boundary", "admin_level", "busway", "cycleway", "emergency", "geological",
    "footway", "highway", "lifeguard", "man_made", "military", "natural",
    "parking", "place", "power", "public_transport", "railway", "route",
    "sidewalk", "telecom", "traffic_sign", "water", "waterway",
})
_UNREACHABLE_LANDUSE = frozenset({
    "industrial", "construction", "aquaculture", "farmyard", "flowerbed",
    "depot",
})


def _reaches_mv_places(primary_key: str, primary_value: str) -> bool:
    """False when generic.lua would have dropped such an object on import."""
    if primary_key in _UNREACHABLE_KEYS:
        return False
    return not (primary_key == "landuse" and primary_value in _UNREACHABLE_LANDUSE)


def _is_country(location_set: dict) -> bool:
    """True when the item applies to the country this instance serves.

    NSI locationSets are ISO codes 95% of the time; the remaining *.geojson
    region files are handled by prefix. Resolving them properly would mean
    pulling in location-conflation, a whole JS dependency, to refine a filter
    that already works. Worth re-measuring for a small country: the shortcut
    holds while regional locationSets stay a small share of the items.

    This filter is not optional: eight of McDonald's eleven items are
    amenity=fast_food and differ only by locationSet. Skipping it would give a
    one-in-eight chance of tagging a Lyon McDonald's in Japanese.

    It stays a per-brand filter, not a per-object one: an fx-scoped brand can
    in theory be applied to a Réunion object. Telling them apart would mean
    evaluating geography per POI, for a handful of brands that do not overlap.

    One list holds both the country's own codes and the ones that contain it,
    because both are read the same way: an item scoped to the world applies
    here, and an item excluding the world — or Europe — does not.
    """
    codes = get_country().nsi_locations
    include = [str(x).lower() for x in (location_set.get("include") or [])]
    exclude = [str(x).lower() for x in (location_set.get("exclude") or [])]

    def here(code):
        return code in codes or code.startswith(tuple(f"{c}-" for c in codes))

    return not any(here(code) for code in exclude) and any(
        here(code) for code in include
    )


def _candidates(nsi_json: dict):
    """Every item that could reach mv_places, tags left unfiltered.

    Split out of select_items so the calibration script (scripts/
    calibrate_nsi_tags.py) measures the same population against every NSI tag,
    not only the ones already declared writable.
    """
    for path, category in nsi_json["nsi"].items():
        tree, primary_key, primary_value = path.split("/")
        if tree not in _TREES:
            continue
        if not _reaches_mv_places(primary_key, primary_value):
            continue

        for item in category.get("items", []):
            tags = item["tags"]
            brand_wikidata = tags.get("brand:wikidata")
            if not brand_wikidata:
                continue
            if not _is_country(item.get("locationSet") or {}):
                continue

            yield (
                brand_wikidata,
                tags.get("brand"),
                tags.get("name"),
                primary_key,
                primary_value,
                tags,
            )


def select_items(nsi_json: dict) -> list[tuple]:
    """The nsi_brands rows to insert, from the parsed dist/nsi.json.

    Pure function, no I/O: this is where every selection rule lives, and the
    only thing the tests need.
    """
    writable = get_country().nsi_writable_tags
    candidates = [
        row[:5] + ({k: v for k, v in row[5].items() if k in writable},)
        for row in _candidates(nsi_json)
    ]

    # A brand:wikidata is not a unique key: 2692 QIDs carry several items, and
    # those are the biggest brands. Items differing by category are fine — the
    # object's own primary tag tells them apart. Items sharing a category are
    # only a problem when they disagree on what they would write: Intermarché
    # and Intermarché Drive are both shop=supermarket, and one of them carries
    # drive_through=only. Their labels differ too, but nothing writes those, and
    # both remain useful for recovering the QID from a name.
    #
    # So the group is kept whole when its written tags agree, and dropped whole
    # when they do not — 2 groups out of 2123.
    written = defaultdict(set)
    for row in candidates:
        written[row[0:1] + row[3:5]].add(json.dumps(row[5], sort_keys=True))
    return [row for row in candidates if len(written[row[0:1] + row[3:5]]) == 1]


def _stamp(version: str) -> str:
    """What identifies an import: the NSI release and the code reading it."""
    return f"{version}+{app_version()}"


def _latest_version() -> str:
    """Newest published NSI version, from the npm registry.

    The registry is the authority, not the CDN: jsDelivr answers `@latest` from
    a cache that can be years stale, and the file's own _meta.version then
    describes that stale content rather than the current release.
    """
    resp = requests.get(NSI_REGISTRY_URL, timeout=30)
    resp.raise_for_status()
    return resp.json()["dist-tags"]["latest"]


def _version_date(version: str) -> datetime:
    """Publication date carried by an NSI version (8.0.20260729 -> 2026-07-29)."""
    try:
        return datetime.strptime(version.rsplit(".", 1)[-1], "%Y%m%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return datetime.now(timezone.utc)


def download_nsi():
    conn = connect()
    try:
        last_stamp = last_import_comment(conn, "nsi")
        start_import(conn, "nsi")  # puts the site in maintenance mode

        with unavailable_if_unreachable("NSI"):
            version = _latest_version()
        if _stamp(version) == last_stamp:
            logger.info("NSI already up-to-date (%s), skipping", version)
            record_import(conn, "nsi", _version_date(version), "skipped", _stamp(version))
            return

        NSI_DIR.mkdir(parents=True, exist_ok=True)
        with unavailable_if_unreachable("NSI"):
            download_large_file(NSI_CDN_URL.format(version=version), NSI_PATH)
        logger.info("Downloaded NSI %s", version)
    finally:
        conn.close()


def import_nsi():
    if not NSI_PATH.exists():
        logger.info("No NSI file found, skipping import")
        return

    with open(NSI_PATH) as infile:
        nsi_json = json.load(infile)

    rows = select_items(nsi_json)
    version = _latest_version()

    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE nsi_brands")
            with cur.copy(
                "COPY nsi_brands (brand_wikidata, brand, name,"
                " primary_key, primary_value, tags) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row[:-1] + (json.dumps(row[-1]),))
        conn.commit()
        record_import(conn, "nsi", _version_date(version), "success", _stamp(version))
        logger.info(
            "Imported %d NSI brands (%d distinct QIDs)",
            len(rows),
            len({row[0] for row in rows}),
        )
    finally:
        conn.close()

    NSI_PATH.unlink()
