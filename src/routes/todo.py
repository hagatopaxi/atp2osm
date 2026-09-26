import logging
from typing import Any

import psycopg
from flask import Blueprint, Response, abort, render_template, request, session
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from psycopg import sql
from psycopg.rows import dict_row

from src.db import get_osmdb
from src.routes.auth import auth_required
from src.utils import TODO_FILTERS as FILTERS
from src.utils import (
    build_filters,
    fetch_osm_users,
    hide_brands_in_atp,
    order_by,
    parse_sorts,
    where_clause,
)

logger = logging.getLogger(__name__)

todo_bp = Blueprint("todo", __name__)

SORT_COLUMNS = {
    "name": "brand_name",
    "wikidata": "brand_wikidata",
    "estimation": "estimation",
    "user": "osm_user_id",
    "date": "created_at",
}


@todo_bp.route("/todo")
def todo() -> str:
    osmdb = get_osmdb()
    conditions, params, filters = build_filters(request.args, FILTERS)
    conditions = hide_brands_in_atp(conditions, request.args, filters)
    # Biggest brands first by default: that is the work worth doing.
    sorts = parse_sorts(request.args, SORT_COLUMNS, default=[("estimation", True)])
    with osmdb.cursor(row_factory=dict_row) as cursor:
        entries = cursor.execute(
            sql.SQL("SELECT * FROM todo_brands {where} ORDER BY {order}").format(
                where=where_clause(conditions), order=order_by(sorts, SORT_COLUMNS)
            ),
            params,
        ).fetchall()
        counted = cursor.execute("SELECT COUNT(*) AS total FROM todo_brands").fetchone()
        total = int(counted["total"]) if counted else 0
        all_user_ids = [
            int(r["osm_user_id"])
            for r in cursor.execute("SELECT DISTINCT osm_user_id FROM todo_brands").fetchall()
        ]
        updater_ids = [
            int(r["updated_by"])
            for r in cursor.execute(
                "SELECT DISTINCT updated_by FROM todo_brands WHERE updated_by IS NOT NULL"
            ).fetchall()
        ]

    users = fetch_osm_users(list(set(all_user_ids) | set(updater_ids)))
    current_user_id = session["user"]["osm_id"] if "user" in session else None
    return render_template(
        "todo.html",
        entries=entries,
        users=users,
        current_user_id=current_user_id,
        total=total,
        filters=filters,
        sorts=sorts,
        filter_users=sorted(
            ((uid, users.get(uid, str(uid))) for uid in all_user_ids),
            key=lambda u: u[1].lower(),
        ),
    )


@todo_bp.route("/todo/check")
def todo_check() -> ResponseReturnValue:
    wikidata = request.args.get("wikidata", "").strip()
    name = request.args.get("name", "").strip()
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        matches: list[dict[str, Any]] = []
        if wikidata:
            row = cursor.execute(
                "SELECT id, brand_wikidata, brand_name FROM todo_brands WHERE brand_wikidata = %s",
                (wikidata,),
            ).fetchone()
            if row:
                matches.append(dict(row))
        if name and not matches:
            rows = cursor.execute(
                "SELECT id, brand_wikidata, brand_name FROM todo_brands WHERE brand_name ILIKE %s LIMIT 5",
                (f"%{name}%",),
            ).fetchall()
            matches.extend([dict(r) for r in rows])
    return {"matches": matches}


class _Form:
    """The fields of a todo entry, read from the JSON body of the request."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.brand_wikidata = (str(data.get("brand_wikidata") or "")).strip() or None
        self.brand_name = (str(data.get("brand_name") or "")).strip()
        self.estimation: int | None = None
        estimation = data.get("estimation")
        if estimation is not None:
            self.estimation = int(estimation)


def _form() -> _Form | tuple[dict[str, str], int]:
    """The entry the request describes, or the error response refusing it."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return {"error": _("The request body must be a JSON object")}, 400
    try:
        form = _Form(data)  # pyright: ignore[reportUnknownArgumentType] — a JSON object
    except (ValueError, TypeError):
        return {"error": _("The estimation must be a whole number")}, 400
    if not form.brand_name:
        return {"error": _("The brand name is required")}, 400
    return form


@todo_bp.route("/todo", methods=["POST"])
@auth_required
def todo_add() -> ResponseReturnValue:
    form = _form()
    if not isinstance(form, _Form):
        return form
    osm_user_id = session["user"]["osm_id"]
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        try:
            cursor.execute(
                """INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id, estimation)
                   VALUES (%s, %s, %s, %s)""",
                (form.brand_wikidata, form.brand_name, osm_user_id, form.estimation),
            )
            osmdb.commit()
        except psycopg.errors.UniqueViolation:
            osmdb.rollback()
            return {"error": _("This brand is already in the list")}, 409
        except Exception:
            osmdb.rollback()
            logger.exception("Failed to insert todo brand")
            return {"error": _("Something went wrong, please try again.")}, 500
    return Response(status=201)


@todo_bp.route("/todo/<int:entry_id>", methods=["PUT"])
@auth_required
def todo_update(entry_id: int) -> ResponseReturnValue:
    form = _form()
    if not isinstance(form, _Form):
        return form
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        try:
            updated = cursor.execute(
                """UPDATE todo_brands
                   SET brand_wikidata = %s, brand_name = %s, estimation = %s,
                       updated_by = %s, updated_at = NOW()
                   WHERE id = %s RETURNING id""",
                (
                    form.brand_wikidata,
                    form.brand_name,
                    form.estimation,
                    session["user"]["osm_id"],
                    entry_id,
                ),
            ).fetchone()
            osmdb.commit()
        except psycopg.errors.UniqueViolation:
            osmdb.rollback()
            return {"error": _("This brand is already in the list")}, 409
        except Exception:
            osmdb.rollback()
            logger.exception("Failed to update todo brand")
            return {"error": _("Something went wrong, please try again.")}, 500
    if updated is None:
        return Response(status=404)
    return Response(status=204)


@todo_bp.route("/todo/<int:entry_id>", methods=["DELETE"])
@auth_required
def todo_delete(entry_id: int) -> ResponseReturnValue:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        entry = cursor.execute(
            "SELECT osm_user_id FROM todo_brands WHERE id = %s", (entry_id,)
        ).fetchone()
        if entry is None:
            return Response(status=404)
        if entry["osm_user_id"] != session["user"]["osm_id"]:
            return abort(403)
        cursor.execute("DELETE FROM todo_brands WHERE id = %s", (entry_id,))
        osmdb.commit()
    return Response(status=204)
