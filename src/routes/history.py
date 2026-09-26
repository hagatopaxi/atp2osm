import logging
from datetime import UTC, datetime, timedelta

from flask import Blueprint, abort, render_template, request
from psycopg import sql
from psycopg.rows import dict_row

from src.db import get_osmdb
from src.utils import HISTORY_FILTERS as FILTERS
from src.utils import build_filters, fetch_osm_users, order_by, parse_sorts, where_clause

logger = logging.getLogger(__name__)

history_bp = Blueprint("history", __name__)

HISTORY_PER_PAGE = 50

SORT_COLUMNS = {
    "date": "import_date",
    "brand": "brand_name",
    "status": "status",
    "user": "osm_user_id",
    "items": "items_count",
    "subdivisions": "subdivisions_count",
}


@history_bp.route("/history")
def history() -> str:
    osmdb = get_osmdb()
    page = max(1, request.args.get("page", 1, type=int))
    offset = (page - 1) * HISTORY_PER_PAGE
    conditions, params, filters = build_filters(request.args, FILTERS)
    where = where_clause(conditions)
    sorts = parse_sorts(request.args, SORT_COLUMNS, default=[("date", True)])

    with osmdb.cursor(row_factory=dict_row) as cursor:
        counted = cursor.execute(
            sql.SQL("SELECT COUNT(*) AS total FROM import_history {}").format(where), params
        ).fetchone()
        total = int(counted["total"]) if counted else 0

        entries = cursor.execute(
            sql.SQL("""SELECT *,
                       (SELECT COUNT(*) FROM import_subdivisions sub
                        WHERE sub.import_id = import_history.id) AS subdivisions_count
                FROM import_history {where}
                ORDER BY {order}
                LIMIT %s OFFSET %s""").format(where=where, order=order_by(sorts, SORT_COLUMNS)),
            [*params, HISTORY_PER_PAGE, offset],
        ).fetchall()

        all_user_ids = [
            int(r["osm_user_id"])
            for r in cursor.execute("SELECT DISTINCT osm_user_id FROM import_history").fetchall()
        ]

    total_pages = max(1, -(-total // HISTORY_PER_PAGE))
    users = fetch_osm_users(all_user_ids)

    return render_template(
        "history.html",
        entries=entries,
        users=users,
        page=page,
        total_pages=total_pages,
        total=total,
        filters=filters,
        sorts=sorts,
        filter_users=sorted(
            ((uid, users.get(uid, str(uid))) for uid in all_user_ids),
            key=lambda u: u[1].lower(),
        ),
    )


@history_bp.route("/history/<int:entry_id>")
def history_detail(entry_id: int) -> str:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        entry = cursor.execute("SELECT * FROM import_history WHERE id = %s", (entry_id,)).fetchone()

        subdivisions = cursor.execute(
            """SELECT * FROM import_subdivisions
               WHERE import_id = %s ORDER BY subdivision_code""",
            (entry_id,),
        ).fetchall()

    if entry is None:
        abort(404)

    users = fetch_osm_users([entry["osm_user_id"]])
    is_recent = (datetime.now(UTC) - entry["import_date"]) < timedelta(minutes=5)
    # Success rate in subdivisions, only when the detail is known: integrations
    # older than the migration have no child rows.
    success_rate = (
        (sum(1 for d in subdivisions if d["status"] == "success"), len(subdivisions))
        if subdivisions
        else None
    )
    return render_template(
        "history_detail.html",
        entry=entry,
        subdivisions=subdivisions,
        success_rate=success_rate,
        users=users,
        is_recent=is_recent,
    )
