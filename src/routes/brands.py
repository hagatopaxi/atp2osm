import difflib
import json
import logging
import re

from collections import Counter

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_babel import gettext as _
from psycopg.rows import dict_row
from requests_oauthlib import OAuth2Session

from src.db import get_osmdb
from src.extensions import cache
from src.matching import (
    BLOCKED_BRANDS_SQL,
    batch_categories,
    batch_scope,
    current_wave,
    get_all,
    get_blocked_subdivisions,
    get_changes,
    get_filtered,
    get_stats,
    sample_for_review,
    select_batch,
)
from src.osm_history import OsmApiUnavailable, protect_recent_edits
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

# Where two opening_hours values are cut to be compared: a rule (';') or a
# time range (','). The separators are kept, so the pieces re-join verbatim.
_HOURS_SEPARATORS = re.compile(r"([;,])")


def highlight_diff(old: str, new: str) -> tuple[list, list]:
    """The two values as (text, changed) pieces, changed where they differ.

    Two opening_hours strings differ in spaces *and* in a time, and the eye
    reads the space first; marking the pieces that really change is what
    lets a reviewer see the 14:30 -> 14:00. The comparison ignores spaces —
    the display keeps them, the value is shown as it is.
    """
    old_parts = _HOURS_SEPARATORS.split(old)
    new_parts = _HOURS_SEPARATORS.split(new)
    key = lambda parts: [re.sub(r"\s", "", p) for p in parts]  # noqa: E731
    matcher = difflib.SequenceMatcher(None, key(old_parts), key(new_parts), autojunk=False)
    old_out, new_out = [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        changed = tag != "equal"
        old_out.extend((p, changed) for p in old_parts[i1:i2])
        new_out.extend((p, changed) for p in new_parts[j1:j2])
    return old_out, new_out


# Sorted in Python: the list is already in memory, see the comment in brands().
SORT_COLUMNS = {
    "wikidata": "brand_wikidata",
    "brand": "brand",
    "total": "total",
    "status": "last_status",
    "last_import": "last_import",
}


def _get_blocking_import(brand_wikidata: str, wave: int):
    """Changeset-less import still under cooldown on this wave, or None.

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
                WHERE brand_wikidata = %s AND (wave = %s OR status = 'cancelled')
                ORDER BY import_date DESC
                LIMIT 1""",
            (brand_wikidata, wave),
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
def brand_matches(brand_wikidata, wave):
    """Every match of a brand on a wave, whatever its subdivision — the
    expensive part. Keyed on the wave too: the two waves read the same rows but
    produce different proposals."""
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        get_filtered(cursor, brand=brand_wikidata)
        return get_changes(cursor, wave)


def get_batch(brand_wikidata):
    """Matches of the next batch, its scope per subdivision, and its wave.

    Recomposed on every call from the current state: two calls with no import in
    between give the same batch. Each wave brings its own batch size.
    """
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        wave = current_wave(cursor, brand_wikidata)
        blocked = get_blocked_subdivisions(cursor, brand_wikidata, wave.number)

    changes = brand_matches(brand_wikidata, wave.number)
    changes = select_batch(changes, blocked, wave.batch_size)
    # A value a human posted recently is theirs, not ours. Costs no request on
    # a wave that only adds tags, and one batch's worth on wave 2.
    # ponytail: replayed on /validate, /confirm and /upload rather than cached
    # — a batch is one POI in alpha. Memoize it if the batch size is raised.
    changes = protect_recent_edits(changes)
    return changes, batch_scope(changes), wave


@brands_bp.errorhandler(OsmApiUnavailable)
def osm_api_unavailable(error):
    """Wave 2 could not date the values it would overwrite: nothing is decided,
    nothing is recorded. The reviewer comes back when the API does."""
    logger.warning("OSM API unavailable: %s", error)
    if request.method == "POST":
        return Response(
            json.dumps({"errors": ["OSM API unavailable"]}),
            status=503,
            mimetype="application/json",
        )
    return render_template(
        "errors/503.html",
        message=_("OpenStreetMap could not be reached: nothing was changed."),
    ), 503


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
        wave_counts=Counter(r["wave"] for r in all_brands),
    )


@brands_bp.route("/brands/<brand_wikidata>/validate")
@auth_required
# @cache.cached(query_string=True, key_prefix="brands/")
def brands_validate(brand_wikidata):
    changes, scope, wave = get_batch(brand_wikidata)

    if len(changes) == 0:
        osmdb = get_osmdb()
        with osmdb.cursor() as cursor:
            brand_name = cursor.execute(
                "SELECT brand FROM atp_places WHERE brand_wikidata = %s LIMIT 1",
                (brand_wikidata,),
            ).fetchone()
            brand_name = brand_name[0] if brand_name else None
            cursor.execute(
                """INSERT INTO import_history (brand_wikidata, osm_user_id, status, items_count, brand_name, wave)
                   VALUES (%s, %s, 'success', 0, %s, %s)""",
                (brand_wikidata, session["user"]["osm_id"], brand_name, wave.number),
            )
            osmdb.commit()
        return render_template("brands/:brand_wikidata/empty.html")

    items = sample_for_review(changes, wave.sample_size)
    brand = items[0]["atp_brand"]
    for idx, item in enumerate(items):
        item["title"] = (
            f"{item['tag'].get('name') or item['atp_brand']} - {item['postcode']}"
        )
        # Added, replaced, and the two together — what the review colours in
        # green, and what it shows struck through beside its replacement.
        item["new_tags_keys"] = [
            key for key in item["tag"] if key not in item["old_tag"]
        ]
        item["replaced_tags_keys"] = [
            key
            for key in item["tag"]
            if key in item["old_tag"] and item["tag"][key] != item["old_tag"][key]
        ]
        item["written_tags_keys"] = item["new_tags_keys"] + item["replaced_tags_keys"]
        item["diff"] = {
            key: highlight_diff(item["old_tag"][key], item["tag"][key])
            for key in item["replaced_tags_keys"]
            if key == "opening_hours"
        }
        # Everything the dedicated rows do not show — the NSI tags today,
        # whatever gets added to the sources tomorrow, and the contact:
        # variant of a key when both are written: a row shows one of the two.
        # A tag the reviewer cannot see is a tag they cannot invalidate.
        shown = {
            key if key in item["tag"] else f"contact:{key}" for key in _DETAILED_TAGS
        }
        item["other_new_tags"] = {
            key: item["tag"][key]
            for key in item["written_tags_keys"]
            if key not in shown
        }

    return render_template(
        "brands/:brand_wikidata/validate.html",
        brand_wikidata=brand_wikidata,
        brand=brand,
        size=len(changes),
        scope=scope,
        categories=batch_categories(changes),
        items=items,
        wave_number=wave.number,
        last_import=_get_last_import(brand_wikidata),
    )


@brands_bp.route("/brands/<brand_wikidata>/confirm")
@auth_required
def brands_confirm(brand_wikidata):
    changes, _, wave = get_batch(brand_wikidata)
    # A blocked brand is not in the list: only a forged URL lands here.
    if _get_blocking_import(brand_wikidata, wave.number):
        abort(403)

    if len(changes) == 0:
        return redirect(
            url_for("brands.brands_validate", brand_wikidata=brand_wikidata)
        )

    stats = get_stats(changes)

    return render_template(
        "brands/:brand_wikidata/confirm.html",
        stats=stats,
        wave_number=wave.number,
        logs=json.dumps(changes, indent=4, ensure_ascii=False),
    )


@brands_bp.route("/brands/<brand_wikidata>/rejected")
@auth_required
def brands_rejected(brand_wikidata):
    return render_template("brands/:brand_wikidata/rejected.html")


@brands_bp.route("/brands/<brand_wikidata>/report-error", methods=["POST"])
@auth_required
def report_error(brand_wikidata):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    _, _, wave = get_batch(brand_wikidata)
    comment = data.get("comment", "")
    brand_name = data.get("brand_name", "")
    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, brand_name, wave)
               VALUES (%s, %s, 'cancelled', %s, %s, %s) RETURNING id""",
            (brand_wikidata, session["user"]["osm_id"], comment, brand_name, wave.number),
        )
        entry_id = cursor.fetchone()[0]
        osmdb.commit()
    return Response(json.dumps({"id": entry_id}), status=201, mimetype="application/json")


@brands_bp.route("/brands/<brand_wikidata>/upload", methods=["POST"])
@auth_required
def upload_changes(brand_wikidata):
    changes, _, wave = get_batch(brand_wikidata)
    if _get_blocking_import(brand_wikidata, wave.number):
        return Response(
            json.dumps({"errors": ["Brand under cooldown"]}),
            status=403,
            mimetype="application/json",
        )

    # What select_batch truncates, upload must never exceed: last check before an
    # irreversible send. The size is the wave's — wave 2 sends one POI at a time.
    if len(changes) > wave.batch_size:
        return Response(
            json.dumps({"errors": ["Import too large"]}),
            status=403,
            mimetype="application/json",
        )

    osm_session = OAuth2Session(token=session["token"])
    bulk_upload = BulkUpload(changes, session=osm_session, max_size=wave.batch_size)
    errors = bulk_upload.upload()
    # The changesets are on OSM now: nothing after this line may stop the
    # row that records them. The log is a convenience.
    try:
        bulk_upload.save_log_file()
    except OSError:
        logger.exception("Could not save the log of the run")
    # The uploaded POIs now carry their tags: the next batch must be composed on
    # freshly read matches, not on what we had before sending.
    cache.delete_memoized(brand_matches, brand_wikidata, wave.number)

    error_messages = [msg for _, msg in errors]
    status = _determine_import_status(bulk_upload.results)
    stats = get_stats(bulk_upload.uploaded_changes)

    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        # changeset_ids is no longer filled: the per-subdivision detail now
        # lives in import_subdivisions.
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, items_count, brand_name, tags_count, wave)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                brand_wikidata,
                session["user"]["osm_id"],
                status,
                "; ".join(error_messages) or None,
                len(bulk_upload.uploaded_changes),
                bulk_upload.brand_name,
                json.dumps(stats["by_tag"]),
                wave.number,
            ),
        )
        entry_id = cursor.fetchone()[0]
        cursor.executemany(
            """INSERT INTO import_subdivisions
                   (import_id, subdivision_code, subdivision_name, items_count,
                    osm_changeset_id, status, comment, tag_counts)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    entry_id,
                    r["subdivision_code"],
                    r["subdivision_name"],
                    r["items_count"],
                    r["osm_changeset_id"],
                    r["status"],
                    r["comment"],
                    json.dumps(r["tag_counts"]),
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
