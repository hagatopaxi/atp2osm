import logging
import re
import unicodedata
from typing import Any, Final

import psycopg
from flask import Blueprint, Response, render_template, request, session
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from psycopg import sql
from psycopg.rows import dict_row

from src.db import get_osmdb
from src.extensions import limit
from src.github_issues import IssueState, fetch_issue_state, issue_number
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

_WIKIDATA: Final = re.compile(r"^Q[1-9]\d*$")
# Brand names run to a few dozen characters; a page of them is everyone's.
MAX_NAME_LENGTH: Final = 200

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
        issue_number=issue_number,
    )


@todo_bp.route("/todo/check")
@limit("60/minute")
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


def _visible(text: str) -> str:
    """`text` without control and format characters: no newline in a table
    cell, and no right-to-left override or zero-width character to make one
    entry read as another in everyone's list.
    """
    return "".join(c for c in text if unicodedata.category(c) not in ("Cc", "Cf"))


class _Form:
    """The fields of a todo entry, read from the JSON body of the request."""

    def __init__(self, data: dict[str, Any]) -> None:
        wikidata = (str(data.get("brand_wikidata") or "")).strip()
        # q42 is Q42 typed in a hurry; anything else is echoed back as typed.
        if _WIKIDATA.match(wikidata.upper()):
            wikidata = wikidata.upper()
        self.brand_wikidata = wikidata or None
        self.brand_name = _visible(str(data.get("brand_name") or "")).strip()
        self.github_issue = (str(data.get("github_issue") or "")).strip() or None
        self.estimation: int | None = None
        estimation = data.get("estimation")
        if estimation is not None:
            self.estimation = int(estimation)


def _error(message: str, status: int) -> tuple[dict[str, str], int]:
    return {"error": message}, status


def _invalid(form: _Form) -> str | None:
    """What is wrong with the fields of an entry, None when nothing is."""
    if not form.brand_name:
        return _("The brand name is required")
    if len(form.brand_name) > MAX_NAME_LENGTH:
        return _("The brand name is too long (%(max)d characters at most)", max=MAX_NAME_LENGTH)
    if form.brand_wikidata and not _WIKIDATA.match(form.brand_wikidata):
        return _(
            "“%(code)s” is not a Wikidata code: it is a Q followed by digits, e.g. Q123456",
            code=form.brand_wikidata,
        )
    if form.estimation is not None and form.estimation < 0:
        return _("The estimation cannot be negative")
    if form.github_issue and issue_number(form.github_issue) is None:
        return _(
            "“%(url)s” is not a link to an All The Places issue or pull request, "
            "e.g. https://github.com/alltheplaces/alltheplaces/issues/1234",
            url=form.github_issue,
        )
    return None


def _form() -> _Form | tuple[dict[str, str], int]:
    """The entry the request describes, or the error response saying what is wrong."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error(_("The request body must be a JSON object"), 400)
    try:
        form = _Form(data)  # pyright: ignore[reportUnknownArgumentType] — a JSON object
    except (ValueError, TypeError):
        return _error(_("The estimation must be a whole number"), 400)
    if message := _invalid(form):
        return _error(message, 400)
    return form


def _duplicate(
    cursor: psycopg.Cursor[dict[str, Any]], wikidata: str | None
) -> tuple[dict[str, str], int]:
    """The conflict response naming the entry that already holds the Wikidata code."""
    row = cursor.execute(
        "SELECT brand_name FROM todo_brands WHERE brand_wikidata = %s", (wikidata,)
    ).fetchone()
    if row is None:
        return _error(_("This brand is already in the list"), 409)
    return _error(
        _(
            "The Wikidata code %(code)s is already in the list, as “%(name)s”",
            code=wikidata,
            name=row["brand_name"],
        ),
        409,
    )


def _issue_state(url: str | None) -> IssueState | None:
    return fetch_issue_state(url) if url else None


@todo_bp.route("/todo", methods=["POST"])
@limit("20/minute")
@auth_required
def todo_add() -> ResponseReturnValue:
    form = _form()
    if not isinstance(form, _Form):
        return form
    osm_user_id = session["user"]["osm_id"]
    state = _issue_state(form.github_issue)
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        try:
            cursor.execute(
                """INSERT INTO todo_brands (brand_wikidata, brand_name, osm_user_id, estimation,
                                            github_issue, issue_state, issue_checked_at)
                   VALUES (%s, %s, %s, %s, %s, %s,
                           CASE WHEN %s::text IS NULL THEN NULL ELSE NOW() END)""",
                (
                    form.brand_wikidata,
                    form.brand_name,
                    osm_user_id,
                    form.estimation,
                    form.github_issue,
                    state,
                    state,
                ),
            )
            osmdb.commit()
        except psycopg.errors.UniqueViolation:
            osmdb.rollback()
            return _duplicate(cursor, form.brand_wikidata)
        except Exception:
            osmdb.rollback()
            logger.exception("Failed to insert todo brand")
            return _error(_("The database refused the entry, please try again later."), 500)
    return Response(status=201)


@todo_bp.route("/todo/<int:entry_id>", methods=["PUT"])
@limit("20/minute")
@auth_required
def todo_update(entry_id: int) -> ResponseReturnValue:
    form = _form()
    if not isinstance(form, _Form):
        return form
    state = _issue_state(form.github_issue)
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        try:
            updated = cursor.execute(
                """UPDATE todo_brands
                   SET brand_wikidata = %s, brand_name = %s, estimation = %s,
                       github_issue = %s, issue_state = %s,
                       issue_checked_at = CASE WHEN %s::text IS NULL THEN NULL ELSE NOW() END,
                       updated_by = %s, updated_at = NOW()
                   WHERE id = %s RETURNING id""",
                (
                    form.brand_wikidata,
                    form.brand_name,
                    form.estimation,
                    form.github_issue,
                    state,
                    state,
                    session["user"]["osm_id"],
                    entry_id,
                ),
            ).fetchone()
            osmdb.commit()
        except psycopg.errors.UniqueViolation:
            osmdb.rollback()
            return _duplicate(cursor, form.brand_wikidata)
        except Exception:
            osmdb.rollback()
            logger.exception("Failed to update todo brand")
            return _error(_("The database refused the change, please try again later."), 500)
    if updated is None:
        return _error(_("This entry no longer exists: someone deleted it meanwhile."), 404)
    return Response(status=204)


@todo_bp.route("/todo/<int:entry_id>", methods=["DELETE"])
@limit("20/minute")
@auth_required
def todo_delete(entry_id: int) -> ResponseReturnValue:
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        entry = cursor.execute(
            "SELECT osm_user_id FROM todo_brands WHERE id = %s", (entry_id,)
        ).fetchone()
        if entry is None:
            return _error(_("This entry no longer exists: someone deleted it meanwhile."), 404)
        if entry["osm_user_id"] != session["user"]["osm_id"]:
            return _error(_("Only the contributor who added this entry can delete it."), 403)
        cursor.execute("DELETE FROM todo_brands WHERE id = %s", (entry_id,))
        osmdb.commit()
    return Response(status=204)
