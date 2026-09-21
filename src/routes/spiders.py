from flask import Blueprint, render_template, request
from psycopg.rows import dict_row

from src.db import code_sql, get_osmdb
from src.matching import UNBLOCKED_WAVES_SQL
from src.utils import filter_brands

spiders_bp = Blueprint("spiders", __name__)

# ponytail: the page is being tried out in production by URL only. Flip it to
# list the page in the menu, the sitemap, llms.txt and the docs.
SPIDERS_PAGE_LINKED = False

SORT_COLUMNS = {
    "spider": "spider",
    "updated": "updated_at",
    "scraped": "scraped",
    "matched": "matched",
    "to_integrate": "to_integrate",
    "integrated": "integrated",
    "status": "last_status",
    "last_import": "last_import",
}

# One row per spider of the ATP run that produced a POI of ours. The deposit
# (scraped here, matched) is counted per spider: every POI carries its
# spider_id. The history is per brand — import_history knows no spider — so a
# spider borrows the integrations of the brands it produces — and what is
# left to integrate, which is what /brands would show for them summed over
# every wave, cooldowns deducted (a POI on both waves counts twice, as two
# integrations).
# ponytail: a brand two spiders produce (maes_dkv and the fuel brands) counts
# on both; a plain sum, as asked. Stamp spider_id on import_history the day
# the shared count misleads.
SPIDERS_SQL = f"""
    WITH brands AS (
        SELECT DISTINCT spider_id, brand_wikidata, brand
        FROM atp_places
        WHERE brand_wikidata IS NOT NULL
    ),
    last AS (
        SELECT DISTINCT ON (brand_wikidata) brand_wikidata, status, import_date
        FROM import_history
        ORDER BY brand_wikidata, import_date DESC
    ),
    integrated AS (
        SELECT brand_wikidata, SUM(items_count) AS items
        FROM import_history
        GROUP BY 1
    ),
    to_integrate AS (
        SELECT brand_wikidata, SUM(total) AS total
        FROM ({UNBLOCKED_WAVES_SQL}) unblocked
        GROUP BY 1
    ),
    per_spider AS (
        SELECT b.spider_id,
               STRING_AGG(b.brand, ' / ' ORDER BY b.brand)       AS brands,
               STRING_AGG(b.brand_wikidata, ' ' ORDER BY b.brand) AS wikidata,
               JSON_AGG(JSON_BUILD_ARRAY(b.brand_wikidata, b.brand) ORDER BY b.brand) AS brand_list,
               SUM(i.items)                                      AS integrated,
               SUM(t.total)                                      AS to_integrate,
               (ARRAY_AGG(l.status ORDER BY l.import_date DESC NULLS LAST))[1]      AS last_status,
               (ARRAY_AGG(l.import_date ORDER BY l.import_date DESC NULLS LAST))[1] AS last_import
        FROM brands b
        LEFT JOIN last l USING (brand_wikidata)
        LEFT JOIN integrated i USING (brand_wikidata)
        LEFT JOIN to_integrate t USING (brand_wikidata)
        GROUP BY b.spider_id
    )
    SELECT s.spider, s.filename, s.errors, s.features, s.updated_at,
           (SELECT COUNT(*) FROM atp_places p WHERE p.spider_id = s.spider) AS scraped,
           COALESCE(m.matched, 0)     AS matched,
           COALESCE(ps.to_integrate, 0) AS to_integrate,
           COALESCE(ps.integrated, 0) AS integrated,
           ps.brands, ps.wikidata, ps.brand_list, ps.last_status, ps.last_import
    FROM atp_spiders s
    LEFT JOIN mv_places_spider m ON m.spider_id = s.spider
    LEFT JOIN per_spider ps ON ps.spider_id = s.spider
"""  # noqa: S608 — composed from a code constant


@spiders_bp.route("/spiders")
def spiders() -> str:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        all_spiders = cursor.execute(code_sql(SPIDERS_SQL)).fetchall()
    # In memory like /brands: a few hundred rows, and the badges count the
    # unfiltered set.
    rows, filters = filter_brands(
        all_spiders, request.args, search=("spider", "brands", "wikidata")
    )
    run = request.args.get("run")
    if run in ("ok", "failed"):
        rows = [r for r in rows if (r["errors"] == 0) == (run == "ok")]
        filters["run"] = run
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
        filters=filters,
        sort=sort,
        direction=direction,
    )
