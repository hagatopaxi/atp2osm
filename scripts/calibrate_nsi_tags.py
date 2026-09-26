"""Measure, tag by tag, how often NSI agrees with the OSM objects of a country.

NSI_WRITABLE_TAGS is not a universal constant: it was produced by measuring
agreement on French data, and a tag that misbehaves elsewhere produces no
error, only thousands of badly tagged objects. So the list is measured again
for every country — see specs/03_internationalisation.md §6.

    OSM_DB_NAME=… OSM_DB_USER=… OSM_DB_PASSWORD=… OSM_DB_HOST=… OSM_DB_PORT=… \
        uv run python scripts/calibrate_nsi_tags.py countries/fr.json

Prints the table and, unless --dry-run, writes the two keys of the country
file: `nsi_writable_tags` (the tags above the threshold) and `nsi_calibration`
(the whole measurement that justifies the selection). Nobody copies a rate by
hand; a new NSI release is re-evaluated by rerunning this and diffing.

Read-only on the database: everything it creates lives in a schema it drops on
the way out.
"""

import argparse
import json
import pathlib
import re
import sys
from typing import Any, LiteralString, cast

import psycopg
from psycopg import sql

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from src.config import get_database
from src.pipeline.constants import NSI_CDN_URL, NSI_DIR, NSI_PATH
from src.pipeline.nsi import (
    Tags,
    candidate_items,
    is_country,
    latest_version,
)
from src.utils import download_large_file

SCHEMA: LiteralString = "nsi_calibration"

# brand:wikidata is the join key and the only tag whose correctness does not
# depend on the language, so it is writable in every country by construction.
ALWAYS_WRITABLE = "brand:wikidata"


def load_nsi(path: pathlib.Path | None) -> dict[str, Any]:
    """The NSI dump, downloaded if the pipeline has already consumed its copy."""
    path = path or NSI_PATH
    if not path.exists():
        NSI_DIR.mkdir(parents=True, exist_ok=True)
        download_large_file(NSI_CDN_URL.format(version=latest_version()), path)
    with path.open() as infile:
        return json.load(infile)


# What a group of NSI items sharing a (QID, category) agrees on.
Group = dict[str, Any]
Row = tuple[str, str | None, str | None, str, str, str]


def calibration_rows(nsi_json: dict[str, Any]) -> list[Row]:
    """nsi_brands rows carrying every NSI tag, not only the writable ones.

    Same grouping rule as select_items, applied per key instead of per row: a
    (QID, category) group keeps the tags all its items agree on, and drops the
    ones they disagree about. Measuring a tag whose value depends on which item
    of the group is picked would measure the coin toss, not the tag.
    """
    groups: dict[tuple[str, str, str], Group] = {}
    for qid, brand, name, key, value, tags in candidate_items(nsi_json):
        group = groups.setdefault((qid, key, value), {"labels": (brand, name), "tags": None})
        agreed: Tags | None = group["tags"]
        group["tags"] = (
            tags if agreed is None else {k: v for k, v in agreed.items() if tags.get(k) == v}
        )
    return [
        (qid, group["labels"][0], group["labels"][1], key, value, json.dumps(group["tags"]))
        for (qid, key, value), group in groups.items()
    ]


def regional_location_share(nsi_json: dict[str, Any]) -> tuple[int, int]:
    """Items the country keeps whose scope is a region, not the country.

    is_country matches fr-ara.geojson and fr-75 by prefix, so a regionally
    scoped item is not dropped — it is applied to the whole country. Harmless
    when the region is a slice of a large country, wrong when regional scoping
    is most of what NSI carries there. Above 10%, the shortcut needs revisiting
    and a real geometry engine (location-conflation).
    """

    def regional(code: object) -> bool:
        code = str(code)
        return ("-" in code or code.endswith(".geojson")) and is_country({"include": [code]})

    kept = regionally_scoped = 0
    categories: dict[str, dict[str, Any]] = nsi_json["nsi"]
    for category in categories.values():
        items: list[dict[str, Any]] = category.get("items", [])
        for item in items:
            location_set: dict[str, Any] = item.get("locationSet") or {}
            if not is_country(location_set):
                continue
            kept += 1
            included: list[object] = location_set.get("include") or []
            regionally_scoped += any(regional(code) for code in included)
    return regionally_scoped, kept


def install(cur: psycopg.Cursor[Any], rows: list[Row]) -> None:
    """A copy of nsi_brands and of the matching functions, inside SCHEMA.

    The functions come from the catalog rather than from a second copy of their
    source: a migration that changes nsi_match must change what is measured
    here too. Their pinned search_path is rewritten so they read the
    unfiltered table.
    """
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE; CREATE SCHEMA {SCHEMA}")
    cur.execute(f"CREATE TABLE {SCHEMA}.nsi_brands (LIKE public.nsi_brands)")
    with cur.copy(f"COPY {SCHEMA}.nsi_brands FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)
    cur.execute(
        f"CREATE INDEX ON {SCHEMA}.nsi_brands (brand_wikidata);"
        f"CREATE INDEX ON {SCHEMA}.nsi_brands (LOWER(brand), primary_key, primary_value);"
        f"CREATE INDEX ON {SCHEMA}.nsi_brands (LOWER(name), primary_key, primary_value);"
    )
    for name in ("osm_primary_tag", "nsi_match"):
        cur.execute("SELECT pg_get_functiondef(%s::regproc)", (f"public.{name}",))
        found = cur.fetchone()
        body = str(found[0]) if found else ""
        body = body.replace(f"public.{name}(", f"{SCHEMA}.{name}(", 1)
        body, replaced = re.subn(
            r"SET search_path TO 'public'", f"SET search_path TO '{SCHEMA}', 'public'", body
        )
        # Without the rewrite the copy reads the real, already-filtered table
        # and every non-writable tag measures as absent — a silent empty result.
        if replaced != 1:
            raise SystemExit(f"{name} no longer pins its search_path as expected")
        # A definition migrations of this repository wrote, read back from the
        # catalog and renamed with this script's constants: code, though it
        # comes out of the database as text. A maintenance script run by hand,
        # never by the site.
        cur.execute(sql.SQL(cast("LiteralString", body)))


# Measured through nsi_match, which is the point: it is the agreement of what
# would actually be written, not of the raw NSI entry. One consequence to keep
# in mind when reading the table — the primary keys (shop, amenity, office…)
# come out at 100% by construction, since migration 021 already drops a primary
# key the object disagrees with. Their rate confirms that rule works; it does
# not measure the tag.
AGREEMENT_SQL = f"""
    SELECT key,
           count(*)                                                AS observed,
           count(*) FILTER (WHERE osm.tags->>key = nsi.tags->>key) AS agreeing
      FROM mv_places osm
      CROSS JOIN LATERAL {SCHEMA}.nsi_match(osm.tags) AS nsi(tags)
      CROSS JOIN LATERAL jsonb_object_keys(nsi.tags) AS key
     WHERE osm.tags ? key
     GROUP BY key
     ORDER BY count(*) FILTER (WHERE osm.tags->>key = nsi.tags->>key)::float
              / count(*) DESC, 2 DESC
"""  # noqa: S608 — the schema name is a constant of this script


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "country_file", type=pathlib.Path, help="country configuration file, created if absent"
    )
    parser.add_argument("--nsi-file", type=pathlib.Path, default=None)
    parser.add_argument(
        "--threshold",
        type=float,
        default=98.0,
        help="agreement rate a tag needs to become writable",
    )
    parser.add_argument(
        "--min-observed",
        type=int,
        default=50,
        help="objects a tag needs before its rate means anything",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the table without touching the country file"
    )
    args = parser.parse_args()

    nsi_json = load_nsi(args.nsi_file)
    regional, total = regional_location_share(nsi_json)

    with psycopg.connect(get_database().conninfo) as conn:
        with conn.cursor() as cur:
            install(cur, calibration_rows(nsi_json))
            cur.execute(AGREEMENT_SQL)
            measures: list[tuple[str, int, int]] = [
                (str(key), int(observed), int(agreeing)) for key, observed, agreeing in cur
            ]
        # DDL is transactional in PostgreSQL: the rollback is what removes the
        # schema, and it is also what keeps the run read-only if it crashes.
        conn.rollback()

    calibration = {
        key: {
            "observed": observed,
            "agreeing": agreeing,
            "rate": round(100 * agreeing / observed, 2),
        }
        for key, observed, agreeing in measures
    }
    writable = sorted(
        {ALWAYS_WRITABLE}
        | {
            key
            for key, m in calibration.items()
            if m["rate"] >= args.threshold and m["observed"] >= args.min_observed
        }
    )

    print(f"{'tag':<28} {'observed':>9} {'agreeing':>9} {'rate':>7}  writable")
    for key, measure in calibration.items():
        print(
            f"{key:<28} {measure['observed']:>9} {measure['agreeing']:>9}"
            f" {measure['rate']:>6.2f}%  {'yes' if key in writable else ''}"
        )
    print(
        f"\nItems scoped to a region rather than the country: {regional}/{total}"
        f" ({100 * regional / total:.1f}%)"
        f"{'  -- above 10%: the is_country shortcut no longer holds' if regional > total / 10 else ''}"
    )

    if args.dry_run:
        return
    config: dict[str, Any] = {}
    if args.country_file.exists():
        config = json.loads(args.country_file.read_text())
    config["nsi_writable_tags"] = writable
    config["nsi_calibration"] = calibration
    args.country_file.parent.mkdir(parents=True, exist_ok=True)
    args.country_file.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    print(f"\nWrote nsi_writable_tags ({len(writable)} tags) to {args.country_file}")


if __name__ == "__main__":
    main()
