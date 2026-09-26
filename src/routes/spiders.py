import json

from flask import Blueprint, render_template, request
from psycopg.rows import dict_row

from src.db import get_osmdb
from src.utils import filter_brands

spiders_bp = Blueprint("spiders", __name__)

# Lists the page in the menu, the sitemap, llms.txt and the docs.
SPIDERS_PAGE_LINKED = True

SORT_COLUMNS = {
    "spider": "spider",
    "updated": "updated_at",
    "scraped": "scraped",
    "matched": "matched",
    "match_rate": "match_rate",
    "status": "last_status",
    "last_import": "last_import",
}

# One row per spider of the ATP run that produced a POI of ours. The deposit
# (scraped, matched) is counted per spider: every POI carries its spider_id.
# The history is per brand — import_history knows no spider — so a spider
# borrows the last integration of the brands it produces.
SPIDERS_SQL = """
    WITH brands AS (
        SELECT DISTINCT spider_id, brand_wikidata, brand
        FROM atp_places
        WHERE brand_wikidata IS NOT NULL
    ),
    last AS (
        SELECT DISTINCT ON (brand_wikidata) brand_wikidata, id, status, import_date, comment
        FROM import_history
        ORDER BY brand_wikidata, import_date DESC
    ),
    per_spider AS (
        SELECT b.spider_id,
               STRING_AGG(b.brand, ' / ' ORDER BY b.brand)       AS brands,
               STRING_AGG(b.brand_wikidata, ' ' ORDER BY b.brand) AS wikidata,
               JSON_AGG(JSON_BUILD_ARRAY(b.brand_wikidata, b.brand) ORDER BY b.brand) AS brand_list,
               (ARRAY_AGG(l.status ORDER BY l.import_date DESC NULLS LAST))[1]      AS last_status,
               (ARRAY_AGG(l.import_date ORDER BY l.import_date DESC NULLS LAST))[1] AS last_import,
               (ARRAY_AGG(l.id ORDER BY l.import_date DESC NULLS LAST))[1]          AS last_id,
               (ARRAY_AGG(l.comment ORDER BY l.import_date DESC NULLS LAST))[1]     AS last_comment
        FROM brands b
        LEFT JOIN last l USING (brand_wikidata)
        GROUP BY b.spider_id
    )
    SELECT s.spider, s.filename, s.errors, s.features, s.updated_at, s.log_url,
           p.scraped,
           COALESCE(m.matched, 0)     AS matched,
           ROUND(100.0 * COALESCE(m.matched, 0) / NULLIF(p.scraped, 0)) AS match_rate,
           ps.brands, ps.wikidata, ps.brand_list, ps.last_status, ps.last_import,
           ps.last_id, ps.last_comment
    FROM atp_spiders s
    CROSS JOIN LATERAL (
        SELECT COUNT(*) AS scraped FROM atp_places p WHERE p.spider_id = s.spider
    ) p
    LEFT JOIN mv_places_spider m ON m.spider_id = s.spider
    LEFT JOIN per_spider ps ON ps.spider_id = s.spider
"""


def cancellation_reasons(comment: str | None) -> tuple[list[str], list[str]]:
    """Reason keys and free-text notes of a cancellation, first seen first.

    The comment is the JSON list rejected.js posts, one entry per POI turned
    down; a cancellation older than the quick-pick reasons is plain text.
    """
    try:
        entries = json.loads(comment or "[]")
    except json.JSONDecodeError:
        return [], [comment] if comment else []
    if not isinstance(entries, list):
        return [], []
    reasons: dict[str, None] = {}
    notes: dict[str, None] = {}
    for entry in entries:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(entry, dict):
            reasons.update(dict.fromkeys(entry.get("reasons") or []))  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
            if entry.get("comment"):  # pyright: ignore[reportUnknownMemberType]
                notes[str(entry["comment"])] = None  # pyright: ignore[reportUnknownArgumentType]
    return list(reasons), list(notes)


@spiders_bp.route("/spiders")
def spiders() -> str:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        all_spiders = cursor.execute(SPIDERS_SQL).fetchall()
    for row in all_spiders:
        row["reasons"], row["notes"] = (
            cancellation_reasons(row["last_comment"])
            if row["last_status"] == "cancelled"
            else ([], [])
        )
    # In memory like /brands: a few hundred rows, and the badges count the
    # unfiltered set.
    rows, filters = filter_brands(
        all_spiders, request.args, search=("spider", "brands", "wikidata")
    )
    run = request.args.get("run")
    if run in ("ok", "failed"):
        rows = [r for r in rows if (r["errors"] == 0) == (run == "ok")]
        filters["run"] = run
    reason = request.args.get("reason")
    if reason:
        rows = [r for r in rows if reason in r["reasons"]]
        filters["reason"] = reason
    sort = request.args.get("sort", "updated")
    sort = sort if sort in SORT_COLUMNS else "updated"
    direction = "asc" if request.args.get("dir") == "asc" else "desc"
    key = SORT_COLUMNS[sort]
    rows = sorted(rows, key=lambda r: (r[key] is None, r[key]), reverse=direction == "desc")
    return render_template(
        "spiders.html",
        rows=rows,
        total_spiders=len(all_spiders),
        shown=len(rows),
        reasons_found=sorted({key for r in all_spiders for key in r["reasons"]}),
        filters=filters,
        sort=sort,
        direction=direction,
    )
