"""Open export API for the data atp2osm itself produces.

Only the tables written by the app are exposed: integration history, its
per-subdivision detail, and the brands-to-do list. Nothing coming from ATP or
OSM (or derived from them) is exported — those upstreams publish their own data.
"""

import csv
import json
import logging
from io import StringIO
from typing import LiteralString

from flask import Blueprint, Response, redirect, request, url_for
from flask.typing import ResponseReturnValue
from psycopg import sql
from psycopg.rows import dict_row

from src.db import get_osmdb
from src.utils import (
    HISTORY_FILTERS,
    TODO_FILTERS,
    FilterSpec,
    build_filters,
    hide_brands_in_atp,
    where_clause,
)

logger = logging.getLogger(__name__)

export_bp = Blueprint("export", __name__)

# dataset name -> (columns, table, filter spec, ORDER BY)
DATASETS: dict[str, tuple[tuple[str, ...], str, FilterSpec, LiteralString]] = {
    "history": (
        (
            "id",
            "brand_wikidata",
            "brand_name",
            "osm_user_id",
            "import_date",
            "status",
            "items_count",
            "tags_count",
        ),
        "import_history",
        HISTORY_FILTERS,
        "import_date DESC",
    ),
    "subdivisions": (
        (
            "id",
            "import_id",
            "subdivision_code",
            "subdivision_name",
            "items_count",
            "osm_changeset_id",
            "status",
            "comment",
        ),
        "import_subdivisions",
        {},
        "import_id DESC, subdivision_code",
    ),
    "todo": (
        (
            "id",
            "brand_wikidata",
            "brand_name",
            "osm_user_id",
            "created_at",
            "estimation",
            "updated_by",
            "updated_at",
        ),
        "todo_brands",
        TODO_FILTERS,
        "created_at DESC",
    ),
}

# One response per table: they hold a few thousand rows at most. A LIMIT or a
# cursor becomes necessary once one outgrows a single response.


@export_bp.route("/api/export/departements.<fmt>")
def export_departements(fmt: str) -> ResponseReturnValue:
    """The dataset was renamed; the old URL is documented publicly."""
    return redirect(url_for("export.export", dataset="subdivisions", fmt=fmt), 301)


@export_bp.route("/api/export/<dataset>.<fmt>")
def export(dataset: str, fmt: str) -> ResponseReturnValue:
    if dataset not in DATASETS or fmt not in ("json", "csv"):
        return {"error": "Unknown dataset or format"}, 404

    columns, table, filter_spec, order_by = DATASETS[dataset]
    conditions, params, _ = build_filters(request.args, filter_spec)
    if dataset == "todo":
        conditions = hide_brands_in_atp(conditions, request.args)

    with get_osmdb().cursor(row_factory=dict_row) as cursor:
        rows = cursor.execute(
            sql.SQL("SELECT {columns} FROM {table} {where} ORDER BY {order_by}").format(
                columns=sql.SQL(", ").join(map(sql.Identifier, columns)),
                table=sql.Identifier(table),
                where=where_clause(conditions),
                order_by=sql.SQL(order_by),
            ),
            params,
        ).fetchall()

    if fmt == "json":
        body = json.dumps(rows, default=str, ensure_ascii=False)
        mimetype = "application/json"
    else:
        buffer = StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: csv_value(v) for k, v in row.items()})
        body = buffer.getvalue()
        mimetype = "text/csv"

    return Response(
        body,
        mimetype=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="atp2osm-{dataset}.{fmt}"',
            "Access-Control-Allow-Origin": "*",  # open API: usable from any client
            # Open to clients, but kept out of the index: the export button
            # sows links to these URLs in the page HTML, which robots.txt alone
            # does not keep from being indexed once discovered.
            "X-Robots-Tag": "noindex",
        },
    )


def csv_value(value: object) -> object:
    """Flatten the values CSV has no cell type for (JSON objects, arrays)."""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value
