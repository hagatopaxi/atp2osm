import logging
from datetime import date

import json

from flask import Blueprint, Response, render_template, request
from psycopg.rows import dict_row

from src.db import get_osmdb
from flask_babel import format_date
from src.matching import BLOCKED_BRANDS_SQL, WAVES_BY_NUMBER
from src.utils import TODO_NOT_IN_ATP_SQL, build_filters, fetch_osm_users

logger = logging.getLogger(__name__)

stats_bp = Blueprint("stats", __name__)

FILTERS = {
    "q": ("brand_name", "brand_wikidata"),
    "user": "osm_user_id",
    "wave": "wave",
    "date": "import_date",
}

TOP_N = 15

KPI_SQL = """
    SELECT COALESCE(SUM(items_count), 0)::int AS pois,
           COUNT(*)                           AS imports,
           COUNT(DISTINCT brand_wikidata)     AS brands
    FROM import_history {where}
"""

# generate_series keeps the periods without any integration in the result.
# {unit}, {start} and {end} are built by _period(), never taken from the request.
SERIES_SQL = """
    WITH periods AS (
        SELECT generate_series(
            {start},
            {end},
            '1 {unit}'
        )::date AS period
    )
    SELECT p.period,
           COALESCE(SUM(h.items_count), 0)::int AS pois,
           COUNT(h.id)                          AS imports,
           {per_wave}
    FROM periods p
    LEFT JOIN import_history h
           ON date_trunc('{unit}', h.import_date)::date = p.period
          {extra}
    GROUP BY p.period
    ORDER BY p.period
"""

# One column per wave, so the chart stacks additions and modifications.
PER_WAVE_SQL = ", ".join(
    f"COALESCE(SUM(h.items_count) FILTER (WHERE h.wave = {w}), 0)::int AS wave_{w}"
    for w in WAVES_BY_NUMBER
)

# tags_count is a JSONB map {tag: number of POIs where it was added}. Split by
# wave: a tag added in wave 1 and overwritten in wave 2 is two readings.
TAGS_SQL = """
    SELECT t.key AS label, h.wave, SUM(t.value::int)::int AS value
    FROM import_history h,
         LATERAL jsonb_each_text(COALESCE(h.tags_count, '{{}}'::jsonb)) t
    {where}
    GROUP BY 1, 2
"""

BRANDS_SQL = """
    SELECT COALESCE(h.brand_name, h.brand_wikidata) AS label,
           h.wave,
           SUM(h.items_count)::int                  AS value
    FROM import_history h {where}
    GROUP BY 1, 2
    HAVING SUM(h.items_count) > 0
"""

# Two ways of contributing, ranked separately: integrations per wave, and
# reports of missing brands, which belong to no wave. The brand filter reaches
# todo_brands too, the period one applies to created_at there.
USERS_SQL = """
    SELECT osm_user_id                        AS label,
           wave,
           COUNT(*)::int                      AS imports,
           COALESCE(SUM(items_count), 0)::int AS pois,
           0                                  AS todos
    FROM import_history {where}
    GROUP BY 1, 2
    UNION ALL
    SELECT osm_user_id, NULL, 0, 0, COUNT(*)::int
    FROM (SELECT osm_user_id, brand_name, brand_wikidata,
                 created_at AS import_date,
                 -- A report belongs to no wave: the wave filter drops it.
                 NULL::smallint AS wave
          FROM todo_brands) todo_brands
    {where}
    GROUP BY 1
"""

# Same reading as SPIDERS_SQL, period by period: what counts is the brand, not
# how many batches it took.
SPIDER_SERIES_SQL = """
    WITH periods AS (
        SELECT generate_series(
            {start},
            {end},
            '1 {unit}'
        )::date AS period
    ),
    per_brand AS (
        SELECT date_trunc('{unit}', h.import_date)::date            AS period,
               h.brand_wikidata,
               COUNT(*) FILTER (WHERE h.status IN ('success', 'partial')) AS ok,
               COUNT(*) FILTER (WHERE h.status = 'cancelled')             AS ko
        FROM import_history h
        {where}
        GROUP BY 1, 2
    )
    SELECT p.period,
           COUNT(b.brand_wikidata) FILTER (WHERE b.ok > 0)              AS integrated,
           COUNT(b.brand_wikidata) FILTER (WHERE b.ok = 0 AND b.ko > 0) AS rejected
    FROM periods p
    LEFT JOIN per_brand b ON b.period = p.period
    GROUP BY p.period
    ORDER BY p.period
"""

# One spider = one ATP brand, and a cancelled integration is a human refusing
# what it produced — the reliability signal.
SPIDERS_SQL = """
    SELECT COALESCE(brand_name, brand_wikidata)                      AS label,
           COUNT(*) FILTER (WHERE status IN ('success', 'partial'))  AS integrated,
           COUNT(*) FILTER (WHERE status = 'cancelled')              AS cancelled
    FROM import_history {where}
    GROUP BY 1
"""

# One import_subdivisions row = one changeset. A refused changeset says nothing
# about the spider: it is the local OSM copy that has drifted since the import.
CHANGESETS_SQL = """
    WITH periods AS (
        SELECT generate_series(
            {start},
            {end},
            '1 {unit}'
        )::date AS period
    )
    SELECT p.period,
           COUNT(d.id) FILTER (WHERE d.status = 'success')  AS ok,
           COUNT(d.id) FILTER (WHERE d.status <> 'success') AS ko
    FROM periods p
    LEFT JOIN import_history h
           ON date_trunc('{unit}', h.import_date)::date = p.period
          {extra}
    LEFT JOIN import_subdivisions d ON d.import_id = h.id
    GROUP BY p.period
    ORDER BY p.period
"""

# Two counts of the present, not of the period: the brands reported missing
# that ATP still lacks, and the brands turned down that no spider has fixed
# since — the same readings as the todo list and the brands list.
MISSING_SQL = f"SELECT COUNT(*) FROM todo_brands WHERE {TODO_NOT_IN_ATP_SQL}"
AWAITING_FIX_SQL = f"""
    SELECT COUNT(DISTINCT brand_wikidata)
    FROM ({BLOCKED_BRANDS_SQL}) b
    WHERE b.status = 'cancelled'
"""


@stats_bp.route("/stats")
def stats():
    return render_template("stats.html", **compute(request.args))


@stats_bp.route("/api/stats.json")
def stats_api():
    """The figures of the statistics page, as JSON, same filters."""
    return Response(
        json.dumps(compute(request.args), default=str, ensure_ascii=False),
        mimetype="application/json",
        headers={"Access-Control-Allow-Origin": "*", "X-Robots-Tag": "noindex"},
    )


def compute(args):
    """Everything the statistics page shows, filtered by the request args."""
    osmdb = get_osmdb()
    where, params, filters = build_filters(args, FILTERS)

    unit, start, end = _period(filters.get("from"), filters.get("to"))

    # Queries that name import_history explicitly need the qualified clause.
    aliased = _alias(where)

    with osmdb.cursor(row_factory=dict_row) as cursor:
        kpi = cursor.execute(KPI_SQL.format(where=where), params).fetchone()
        series = cursor.execute(
            SERIES_SQL.format(
                unit=unit, start=start, end=end, per_wave=PER_WAVE_SQL,
                extra=aliased.replace("WHERE ", "AND ", 1),
            ),
            params,
        ).fetchall()
        tags = cursor.execute(TAGS_SQL.format(where=aliased), params).fetchall()
        brands = cursor.execute(BRANDS_SQL.format(where=aliased), params).fetchall()
        # The clause appears twice in the query, so its params do too.
        users = cursor.execute(USERS_SQL.format(where=where), params * 2).fetchall()
        spiders = cursor.execute(SPIDERS_SQL.format(where=where), params).fetchall()
        spider_series = cursor.execute(
            SPIDER_SERIES_SQL.format(unit=unit, start=start, end=end, where=aliased), params
        ).fetchall()
        changesets = cursor.execute(
            CHANGESETS_SQL.format(
                unit=unit, start=start, end=end, extra=aliased.replace("WHERE ", "AND ", 1)
            ),
            params,
        ).fetchall()
        missing = cursor.execute(MISSING_SQL).fetchone()["count"]
        awaiting_fix = cursor.execute(AWAITING_FIX_SQL).fetchone()["count"]
        all_user_ids = [
            r["osm_user_id"]
            for r in cursor.execute(
                "SELECT osm_user_id FROM import_history"
                " UNION SELECT osm_user_id FROM todo_brands"
            ).fetchall()
        ]

    names = fetch_osm_users(all_user_ids)
    for row in users:
        row["label"] = names.get(row["label"], str(row["label"]))

    # Three rankings out of one query: integrations and POIs, split by wave,
    # and missing brands reported. The integrations ship both ways, the
    # switch is pure CSS.
    def rank(key):
        rows = [
            {"label": u["label"], "wave": u["wave"], "value": u[key]} for u in users if u[key]
        ]
        return _stack(rows)[:TOP_N]


    # A spider is rejected only when nothing of it ever made it through: one
    # cancelled batch followed by a successful one is not a bad spider.
    integrated = sum(1 for s in spiders if s["integrated"])
    rejected = sum(1 for s in spiders if not s["integrated"] and s["cancelled"])
    reliability = (
        round(100 * integrated / (integrated + rejected)) if integrated + rejected else None
    )

    for row in spider_series:
        judged = row["integrated"] + row["rejected"]
        # None rather than zero when no brand was judged: an idle period is not
        # a failed one.
        row["reliability"] = round(100 * row["integrated"] / judged) if judged else None

    for row in changesets:
        sent = row["ok"] + row["ko"]
        row["rate"] = round(100 * row["ok"] / sent) if sent else None

    # The bars show the rhythm, the running totals show the progress — one
    # per wave, so additions and modifications each have their curve.
    totals = dict.fromkeys(WAVES_BY_NUMBER, 0)
    for row in series:
        for w in totals:
            totals[w] += row[f"wave_{w}"]
            row[f"cumulative_{w}"] = totals[w]

    all_tags = _stack(tags)
    tags = all_tags[:TOP_N]
    by_imports, by_pois, reporters = rank("imports"), rank("pois"), rank("todos")
    brands = _stack(brands)[:TOP_N]

    # What Chart.js draws, shaped as series. The wave labels are added by the
    # template, where a locale exists to resolve them.
    period = lambda rows: [format_date(r["period"], "short") for r in rows]
    charts = {
        "pace": {
            "labels": period(series),
            "waves": {w: [r[f"wave_{w}"] for r in series] for w in WAVES_BY_NUMBER},
            "cumulative": {w: [r[f"cumulative_{w}"] for r in series] for w in WAVES_BY_NUMBER},
            "imports": [r["imports"] for r in series],
        },
        "by_imports": _ranking(by_imports),
        "by_pois": _ranking(by_pois),
        "tags": _ranking(tags),
        "brands": _ranking(brands),
        # Reports belong to no wave: one plain series.
        "reporters": {"labels": [r["label"] for r in reporters], "values": [r["value"] for r in reporters]},
        "spiders": {
            "labels": period(spider_series),
            "ok": [r["integrated"] for r in spider_series],
            "ko": [r["rejected"] for r in spider_series],
        },
        "changesets": {
            "labels": period(changesets),
            "ok": [r["ok"] for r in changesets],
            "ko": [r["ko"] for r in changesets],
        },
    }

    return dict(
        kpi=kpi,
        # Same definition as the contributors panels below, filters included.
        contributors=len({u["label"] for u in users}),
        missing=missing,
        awaiting_fix=awaiting_fix,
        by_imports=by_imports,
        by_pois=by_pois,
        reporters=reporters,
        series=series,
        series_max=max((r["pois"] for r in series), default=0),
        unit=unit,
        charts=charts,
        tags=tags,
        tags_by_wave={w: sum(t["by_wave"].get(w, 0) for t in all_tags) for w in WAVES_BY_NUMBER},
        brands=brands,
        spider_series=spider_series,
        spiders_reliability=reliability,
        changesets=changesets,
        changesets_max=max((c["ok"] + c["ko"] for c in changesets), default=0),
        filters=filters,
        filter_users=sorted(
            ((uid, names.get(uid, str(uid))) for uid in all_user_ids),
            key=lambda u: u[1].lower(),
        ),
    )


def _stack(rows):
    """Rank (label, wave, value) rows by total, one row per label.

    Each row keeps its split, `by_wave`, for the stacked bar that compares the
    two typologies of change.
    """
    stacked = {}
    for row in rows:
        entry = stacked.setdefault(
            row["label"], {"label": row["label"], "value": 0, "by_wave": {}}
        )
        entry["value"] += row["value"]
        entry["by_wave"][row["wave"]] = row["value"]
    return sorted(stacked.values(), key=lambda r: r["value"], reverse=True)


def _ranking(rows):
    """A stacked ranking, one horizontal bar per label."""
    return {
        "labels": [r["label"] for r in rows],
        "waves": {w: [r["by_wave"].get(w, 0) for r in rows] for w in WAVES_BY_NUMBER},
    }


def _alias(where, alias="h"):
    """Qualify the filtered columns with the import_history alias."""
    for column in ("brand_name", "brand_wikidata", "osm_user_id", "wave", "import_date"):
        where = where.replace(column, f"{alias}.{column}")
    return where



def _period(date_from, date_to):
    """Bar granularity and bounds of the series, from the filtered period.

    The granularity follows from how long the period is, so the charts hold a
    readable number of bars whatever the dates asked for. Both bounds are
    parsed dates, never request text, so they can be inlined in the SQL.
    """
    start = _parse(date_from)
    stop = _parse(date_to) or date.today()
    span = (stop - start).days if start else None

    if span is not None and span <= 15:
        unit = "day"
    elif span is not None and span <= 120:
        unit = "week"
    else:
        unit = "month"

    first = (
        f"date_trunc('{unit}', DATE '{start}')"
        if start
        # No lower bound: the series starts at the first integration ever.
        else f"date_trunc('{unit}', (SELECT MIN(import_date) FROM import_history))"
    )
    return unit, first, f"date_trunc('{unit}', DATE '{stop}')"


def _parse(value):
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None
