import json
import logging

from flask import (
    Blueprint,
    Response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from psycopg.rows import dict_row
from requests_oauthlib import OAuth2Session

from src.db import get_osmdb, maintenance_since
from src.extensions import cache
from src.matching import (
    BATCH_MAX_SIZE,
    BLOCKED_BRANDS_SQL,
    get_all,
    get_blocked_subdivisions,
    get_changes,
    get_filtered,
    get_stats,
    sample_for_review,
    select_batch,
)
from src.routes.auth import auth_required
from src.upload import BulkUpload
from src.utils import (
    _determine_import_status,
    fetch_osm_users,
    filter_brands,
)

logger = logging.getLogger(__name__)

brands_bp = Blueprint("brands", __name__)

# Tags the review page renders with a row of their own, labelled and formatted.
# Anything else the diff adds is listed generically, so that no change ever
# reaches OSM without the reviewer having seen it.
_DETAILED_TAGS = frozenset({
    "brand",
    "brand:wikidata",
    "name",
    "email",
    "phone",
    "website",
    "opening_hours",
})

# Sorted in Python: the list is already in memory, see the comment in brands().
SORT_COLUMNS = {
    "wikidata": "brand_wikidata",
    "brand": "brand",
    "total": "total",
    "status": "last_status",
    "last_import": "last_import",
}


@brands_bp.before_request
def maintenance_guard():
    """Only these routes read the tables the pipeline rebuilds (mv_places,
    mv_places_brand, atp_places) — the rest of the site stays available."""
    since = maintenance_since(get_osmdb())
    if since is not None:
        return render_template("errors/503.html", since=since), 503, {"Retry-After": "900"}
    return None


def _get_blocking_import(brand_wikidata: str):
    """Changeset-less import still under cooldown, or None.

    Only ever blocks the whole brand: a cancellation, or a pre-migration row,
    points at no subdivision in particular. Per-subdivision blocking lives in
    get_blocked_subdivisions().

    Same cooldowns as get_all(): it is the very same query constant.
    """
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        return cursor.execute(
            f"""SELECT id, import_date, status
                FROM ({BLOCKED_BRANDS_SQL}) blocking
                WHERE brand_wikidata = %s
                ORDER BY import_date DESC
                LIMIT 1""",
            (brand_wikidata,),
        ).fetchone()


def _get_last_import(brand_wikidata: str):
    """Latest integration of the brand, or None — shown on /validate so the
    reviewer knows what went wrong last time (status and comments)."""
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        last = cursor.execute(
            """SELECT id, import_date, status, comment, osm_user_id
               FROM import_history
               WHERE brand_wikidata = %s
               ORDER BY import_date DESC
               LIMIT 1""",
            (brand_wikidata,),
        ).fetchone()
    if last:
        last["osm_user_name"] = fetch_osm_users([last["osm_user_id"]]).get(
            last["osm_user_id"]
        )
    return last


# The ST_DWithin join costs seconds on a big brand (5 s for 3 000 matches), and
# /validate, /confirm then /upload all replay it identically. Its result only
# moves with the daily refresh, so it is cached; blocking, which does move after
# an import, is read live below.
MATCHES_TIMEOUT = 30 * 60


@cache.memoize(timeout=MATCHES_TIMEOUT)
def brand_matches(brand_wikidata):
    """Every match of a brand, whatever its subdivision — the expensive part."""
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        get_filtered(cursor, brand=brand_wikidata)
        return get_changes(cursor)


def get_batch(brand_wikidata):
    """Matches of the next batch, and its scope per subdivision.

    Recomposed on every call from the current state: two calls with no import in
    between give the same batch.
    """
    changes = brand_matches(brand_wikidata)
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        blocked = get_blocked_subdivisions(cursor, brand_wikidata)

    return select_batch(changes, blocked)


@brands_bp.route("/brands")
# @cache.cached(key_prefix="brands")
def brands():
    osmdb = get_osmdb()
    # Filtered in Python rather than through a WHERE in get_all(): the page shows
    # the filtered rows AND counts over the unfiltered set (the "Available / All"
    # badges). Filtering in SQL would need a second query for those counts,
    # replaying get_all()'s cooldowns. Worth switching if the list grows enough
    # that fetching it whole costs.
    all_brands = get_all(osmdb)
    rows, filters = filter_brands(all_brands, request.args)
    sort = request.args.get("sort")
    direction = "asc" if request.args.get("dir") == "asc" else "desc"
    if sort in SORT_COLUMNS:
        key = SORT_COLUMNS[sort]
        rows = sorted(
            rows,
            key=lambda r: (r[key] is None, r[key]),
            reverse=direction == "desc",
        )
    return render_template(
        "brands.html",
        rows=rows,
        total_brands=len(all_brands),
        shown=len(rows),
        filters=filters,
        sort=sort,
        direction=direction,
    )


@brands_bp.route("/brands/<brand_wikidata>/validate")
@auth_required
# @cache.cached(query_string=True, key_prefix="brands/")
def brands_validate(brand_wikidata):
    changes, scope = get_batch(brand_wikidata)

    if len(changes) == 0:
        osmdb = get_osmdb()
        with osmdb.cursor() as cursor:
            brand_name = cursor.execute(
                "SELECT brand FROM atp_places WHERE brand_wikidata = %s LIMIT 1",
                (brand_wikidata,),
            ).fetchone()
            brand_name = brand_name[0] if brand_name else None
            cursor.execute(
                """INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, brand_name)
                   VALUES (%s, %s, 'success', 0, %s)""",
                (brand_wikidata, session["user"]["osm_id"], brand_name),
            )
            osmdb.commit()
        return render_template("brands/:brand_wikidata/empty.html")

    items = sample_for_review(changes)
    brand = items[0]["atp_brand"]
    for idx, item in enumerate(items):
        item["title"] = (
            f"{item['tag'].get('name') or item['atp_brand']} - {item['postcode']}"
        )
        item["new_tags_keys"] = [
            key for key in item["tag"] if key not in item["old_tag"]
        ]
        # Everything the template has no dedicated row for — the NSI tags today,
        # whatever gets added to the sources tomorrow. A tag the reviewer cannot
        # see is a tag they cannot invalidate.
        item["other_new_tags"] = {
            key: item["tag"][key]
            for key in item["new_tags_keys"]
            if key not in _DETAILED_TAGS
        }

    return render_template(
        "brands/:brand_wikidata/validate.html",
        brand_wikidata=brand_wikidata,
        brand=brand,
        size=len(changes),
        scope=scope,
        items=items,
        last_import=_get_last_import(brand_wikidata),
    )


@brands_bp.route("/brands/<brand_wikidata>/confirm")
@auth_required
def brands_confirm(brand_wikidata):
    changes, _ = get_batch(brand_wikidata)

    if len(changes) == 0:
        return redirect(
            url_for("brands.brands_validate", brand_wikidata=brand_wikidata)
        )

    stats = get_stats(changes)

    return render_template(
        "brands/:brand_wikidata/confirm.html",
        stats=stats,
        logs=json.dumps(changes, indent=4, ensure_ascii=False),
    )


@brands_bp.route("/brands/<brand_wikidata>/rejected")
@auth_required
def brands_rejected(brand_wikidata):
    return render_template("brands/:brand_wikidata/rejected.html")


@brands_bp.route("/brands/<brand_wikidata>/report-error", methods=["POST"])
@auth_required
def report_error(brand_wikidata):
    data = request.get_json()
    comment = data.get("comment", "")
    brand_name = data.get("brand_name", "")
    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, brand_name)
               VALUES (%s, %s, 'cancelled', %s, %s) RETURNING id""",
            (brand_wikidata, session["user"]["osm_id"], comment, brand_name),
        )
        entry_id = cursor.fetchone()[0]
        osmdb.commit()
    return Response(json.dumps({"id": entry_id}), status=201, mimetype="application/json")


@brands_bp.route("/brands/<brand_wikidata>/upload", methods=["POST"])
@auth_required
def upload_changes(brand_wikidata):
    if _get_blocking_import(brand_wikidata):
        return Response(
            json.dumps({"error": "Forbidden"}),
            status=403,
            mimetype="application/json",
        )

    changes, _ = get_batch(brand_wikidata)
    # What select_batch truncates, upload must never exceed: last check before an
    # irreversible send.
    if len(changes) > BATCH_MAX_SIZE:
        return Response(
            json.dumps({"error": "Import too large"}),
            status=403,
            mimetype="application/json",
        )

    osm_session = OAuth2Session(token=session["token"])
    bulk_upload = BulkUpload(changes, session=osm_session)
    errors = bulk_upload.upload()
    bulk_upload.save_log_file()
    # The uploaded POIs now carry their tags: the next batch must be composed on
    # freshly read matches, not on what we had before sending.
    cache.delete_memoized(brand_matches, brand_wikidata)

    error_messages = [msg for _, msg in errors]
    status = _determine_import_status(bulk_upload.results)
    stats = get_stats(bulk_upload.uploaded_changes)

    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        # changeset_ids is no longer filled: the per-subdivision detail now
        # lives in import_subdivisions.
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, items_count, brand_name, tags_count)
               VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                brand_wikidata,
                session["user"]["osm_id"],
                status,
                "; ".join(error_messages) or None,
                len(bulk_upload.uploaded_changes),
                bulk_upload.brand_name,
                json.dumps(stats["by_tag"]),
            ),
        )
        entry_id = cursor.fetchone()[0]
        cursor.executemany(
            """INSERT INTO import_subdivisions
                   (import_id, subdivision_code, subdivision_name, items_count,
                    osm_changeset_id, status, comment)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    entry_id,
                    r["subdivision_code"],
                    r["subdivision_name"],
                    r["items_count"],
                    r["osm_changeset_id"],
                    r["status"],
                    r["comment"],
                )
                for r in bulk_upload.results
            ],
        )
        osmdb.commit()

    if not errors:
        return Response(
            json.dumps({"id": entry_id}), status=200, mimetype="application/json"
        )
    if bulk_upload.changesets:
        return Response(
            json.dumps({"partial": True, "errors": error_messages, "id": entry_id}),
            status=200,
            mimetype="application/json",
        )
    return Response(
        json.dumps({"errors": error_messages, "id": entry_id}),
        status=422,
        mimetype="application/json",
    )
