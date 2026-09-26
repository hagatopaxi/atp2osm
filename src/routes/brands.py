import difflib
import json
import logging
import re
from collections import Counter
from collections.abc import Sequence
from typing import Any, NamedTuple

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
from flask.typing import ResponseReturnValue
from flask_babel import gettext as _
from psycopg.rows import DictRow, dict_row
from requests_oauthlib import OAuth2Session

from src.db import get_osmdb
from src.extensions import delete_memoized, memoize
from src.matching import (
    BLOCKED_BRANDS_SQL,
    NO_CATEGORY,
    Category,
    Change,
    SubdivisionScope,
    Wave,
    batch_categories,
    batch_scope,
    category_key,
    current_wave,
    exclude_categories,
    get_all,
    get_blocked_subdivisions,
    get_changes,
    get_filtered,
    get_stats,
    sample_for_review,
    select_batch,
)
from src.osm_history import OsmApiUnavailableError, protect_recent_edits
from src.routes.auth import auth_required
from src.upload import BulkUpload
from src.utils import (
    determine_import_status,
    fetch_osm_users,
    filter_brands,
)

logger = logging.getLogger(__name__)

brands_bp = Blueprint("brands", __name__)

# Tags the review page renders with a row of their own, labelled and formatted.
# Anything else the diff adds is listed generically, so that no change ever
# reaches OSM without the reviewer having seen it.
_DETAILED_TAGS = frozenset(
    {
        "brand",
        "brand:wikidata",
        "name",
        "email",
        "phone",
        "website",
        "opening_hours",
    }
)

# Where two opening_hours values are cut to be compared: a rule (';') or a
# time range (','). The separators are kept, so the pieces re-join verbatim.
_HOURS_SEPARATORS = re.compile(r"([;,])")


Pieces = list[tuple[str, bool]]


def highlight_diff(old: str, new: str) -> tuple[Pieces, Pieces]:
    """The two values as (text, changed) pieces, changed where they differ.

    Two opening_hours strings differ in spaces *and* in a time, and the eye
    reads the space first; marking the pieces that really change is what
    lets a reviewer see the 14:30 -> 14:00. The comparison ignores spaces —
    the display keeps them, the value is shown as it is.
    """
    old_parts = _HOURS_SEPARATORS.split(old)
    new_parts = _HOURS_SEPARATORS.split(new)

    def key(parts: list[str]) -> list[str]:
        return [re.sub(r"\s", "", p) for p in parts]

    matcher = difflib.SequenceMatcher(None, key(old_parts), key(new_parts), autojunk=False)
    old_out: Pieces = []
    new_out: Pieces = []
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


def _get_blocking_import(brand_wikidata: str, wave: int) -> DictRow | None:
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
                LIMIT 1""",  # noqa: S608 — a code constant
            (brand_wikidata, wave),
        ).fetchone()


def _atp_brand_name(brand_wikidata: str) -> str | None:
    """The brand's name as ATP writes it, or None when ATP does not know it."""
    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        named = cursor.execute(
            "SELECT brand FROM atp_places WHERE brand_wikidata = %s LIMIT 1",
            (brand_wikidata,),
        ).fetchone()
    return str(named[0]) if named else None


def _get_last_import(brand_wikidata: str) -> DictRow | None:
    """Latest integration of the brand, or None — shown on /validate so the
    reviewer knows what went wrong last time (status and comments).
    """
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        last = cursor.execute(
            """SELECT id, import_date, status, comment, osm_user_id,
                      included_categories, excluded_categories
               FROM import_history
               WHERE brand_wikidata = %s
               ORDER BY import_date DESC
               LIMIT 1""",
            (brand_wikidata,),
        ).fetchone()
    if last:
        last["osm_user_name"] = fetch_osm_users([last["osm_user_id"]]).get(last["osm_user_id"])
    return last


# The ST_DWithin join costs seconds on a big brand (5 s for 3 000 matches), and
# /validate, /confirm then /upload all replay it identically. Its result only
# moves with the daily refresh, so it is cached; blocking, which does move after
# an import, is read live below.
MATCHES_TIMEOUT = 30 * 60


@memoize(timeout=MATCHES_TIMEOUT)
def brand_matches(brand_wikidata: str, wave: int) -> list[Change]:
    """Every match of a brand on a wave, whatever its subdivision — the
    expensive part. Keyed on the wave too: the two waves read the same rows but
    produce different proposals.
    """
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        get_filtered(cursor, brand=brand_wikidata)
        return get_changes(cursor, wave)


class Batch(NamedTuple):
    """What a review works on: the batch itself, and what it was composed from."""

    changes: list[Change]
    scope: list[SubdivisionScope]
    wave: Wave
    categories: list[Category]
    excluded: frozenset[str]
    # True when the exclusions come from the last integration rather than from
    # the form: /validate says so, so nobody drops a type without knowing.
    replayed: bool


def read_excluded(
    brand_wikidata: str, categories: Sequence[Category]
) -> tuple[frozenset[str], bool]:
    """The types to leave out of the batch, and whether they were replayed.

    The form posts the types it keeps — that is what a ticked checkbox sends —
    and a hidden `filtered`, so "every type kept" is distinguishable from "no
    choice made". Without it the last integration's choice is replayed.
    """
    shown = {category["tag"] or NO_CATEGORY for category in categories}
    if request.args.get("filtered"):
        kept = set(request.args.getlist("keep"))
        return frozenset(shown - kept), False
    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        last = cursor.execute(
            """SELECT excluded_categories
               FROM import_history
               WHERE brand_wikidata = %s
               ORDER BY import_date DESC
               LIMIT 1""",
            (brand_wikidata,),
        ).fetchone()
    return frozenset(last[0] or ()) if last else frozenset(), True


def get_batch(brand_wikidata: str) -> Batch:
    """Matches of the next batch, its scope per subdivision, and its wave.

    Recomposed on every call from the current state: two calls with no import in
    between give the same batch. Each wave brings its own batch size.
    """
    osmdb = get_osmdb()
    with osmdb.cursor(row_factory=dict_row) as cursor:
        wave = current_wave(cursor, brand_wikidata)
        blocked = get_blocked_subdivisions(cursor, brand_wikidata, wave.number)

    matches = brand_matches(brand_wikidata, wave.number)
    # The counts the form shows are those of the unfiltered batch: a type the
    # reviewer took out must stay tickable, with the weight it would have had.
    categories = batch_categories(select_batch(matches, blocked, wave.batch_size))
    excluded, replayed = read_excluded(brand_wikidata, categories)
    changes = select_batch(exclude_categories(matches, excluded), blocked, wave.batch_size)
    # A value a human posted recently is theirs, not ours. Costs no request on
    # a wave that only adds tags, and one batch's worth on wave 2.
    # Replayed on /validate, /confirm and /upload: a batch is one POI in alpha,
    # so the replay is cheap. Memoize it if the batch size is raised.
    changes = protect_recent_edits(changes)
    return Batch(changes, batch_scope(changes), wave, categories, excluded, replayed)


@brands_bp.errorhandler(OsmApiUnavailableError)
def osm_api_unavailable(error: OsmApiUnavailableError) -> ResponseReturnValue:
    """Wave 2 could not date the values it would overwrite: nothing is decided,
    nothing is recorded. The reviewer comes back when the API does.
    """
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
def brands() -> str:
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


def _review_item(item: Change) -> dict[str, Any]:
    """The change, plus what the review page derives from it."""
    tag, old_tag = item["tag"], item["old_tag"]
    # Added, replaced, and the two together — what the review colours in
    # green, and what it shows struck through beside its replacement.
    new_keys = [key for key in tag if key not in old_tag]
    replaced_keys = [key for key in tag if key in old_tag and tag[key] != old_tag[key]]
    written_keys = new_keys + replaced_keys
    # Everything the dedicated rows do not show — the NSI tags today,
    # whatever gets added to the sources tomorrow, and the contact:
    # variant of a key when both are written: a row shows one of the two.
    # A tag the reviewer cannot see is a tag they cannot invalidate.
    shown = {key if key in tag else f"contact:{key}" for key in _DETAILED_TAGS}
    return {
        **item,
        "title": f"{tag.get('name') or item['atp_brand']} - {item['postcode']}",
        "new_tags_keys": new_keys,
        "replaced_tags_keys": replaced_keys,
        "written_tags_keys": written_keys,
        "diff": {
            key: highlight_diff(old_tag[key], tag[key])
            for key in replaced_keys
            if key == "opening_hours"
        },
        "other_new_tags": {key: tag[key] for key in written_keys if key not in shown},
    }


@brands_bp.route("/brands/<brand_wikidata>/validate")
@auth_required
def brands_validate(brand_wikidata: str) -> str:
    batch = get_batch(brand_wikidata)
    changes, scope, wave = batch.changes, batch.scope, batch.wave
    # A batch the filter emptied is not a brand that is done: unticking every
    # type would close it as integrated, so the reviewer confirms it instead.
    filtered_out = not changes and bool(batch.categories) and not request.args.get("confirm_empty")

    if not changes and not filtered_out:
        # Nothing left because the reviewer turned every type down is not a
        # wave that is done: `success` would bring the very same matches back
        # once its cooldown expired. `cancelled` holds them until ATP
        # republishes — until the data that produced them changes.
        refused = sorted(batch.excluded) if batch.categories else []
        osmdb = get_osmdb()
        with osmdb.cursor() as cursor:
            cursor.execute(
                """INSERT INTO import_history
                       (brand_wikidata, osm_user_id, status, comment, items_count, brand_name,
                        wave, included_categories, excluded_categories)
                   VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s)""",
                (
                    brand_wikidata,
                    session["user"]["osm_id"],
                    "cancelled" if refused else "success",
                    json.dumps([{"reasons": ["types_all_excluded"], "comment": ", ".join(refused)}])
                    if refused
                    else None,
                    _atp_brand_name(brand_wikidata),
                    wave.number,
                    [],
                    refused,
                ),
            )
            osmdb.commit()
        if refused:
            return render_template("brands/:brand_wikidata/refused.html", excluded=refused)
        return render_template("brands/:brand_wikidata/empty.html")

    sample = sample_for_review(changes, wave.sample_size)
    items = [_review_item(item) for item in sample]

    return render_template(
        "brands/:brand_wikidata/validate.html",
        brand_wikidata=brand_wikidata,
        brand=sample[0]["atp_brand"] if sample else _atp_brand_name(brand_wikidata),
        size=len(changes),
        scope=scope,
        categories=batch.categories,
        excluded=batch.excluded,
        replayed_filter=batch.replayed and bool(batch.excluded),
        filtered_out=filtered_out,
        items=items,
        wave_number=wave.number,
        last_import=_get_last_import(brand_wikidata),
    )


@brands_bp.route("/brands/<brand_wikidata>/confirm")
@auth_required
def brands_confirm(brand_wikidata: str) -> ResponseReturnValue:
    batch = get_batch(brand_wikidata)
    changes, wave = batch.changes, batch.wave
    # A blocked brand is not in the list: only a forged URL lands here.
    if _get_blocking_import(brand_wikidata, wave.number):
        abort(403)

    if len(changes) == 0:
        return redirect(url_for("brands.brands_validate", brand_wikidata=brand_wikidata))

    stats = get_stats(changes)

    return render_template(
        "brands/:brand_wikidata/confirm.html",
        stats=stats,
        categories=batch_categories(changes),
        excluded=sorted(batch.excluded),
        wave_number=wave.number,
        logs=json.dumps(changes, indent=4, ensure_ascii=False),
    )


@brands_bp.route("/brands/<brand_wikidata>/rejected")
@auth_required
def brands_rejected(brand_wikidata: str) -> str:  # noqa: ARG001 — Flask passes the URL's part by name
    return render_template("brands/:brand_wikidata/rejected.html")


@brands_bp.route("/brands/<brand_wikidata>/report-error", methods=["POST"])
@auth_required
def report_error(brand_wikidata: str) -> ResponseReturnValue:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        abort(400)
    body: dict[str, object] = data  # pyright: ignore[reportUnknownVariableType]
    wave = get_batch(brand_wikidata).wave
    comment = str(body.get("comment", ""))
    brand_name = str(body.get("brand_name", ""))
    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, brand_name, wave)
               VALUES (%s, %s, 'cancelled', %s, %s, %s) RETURNING id""",
            (brand_wikidata, session["user"]["osm_id"], comment, brand_name, wave.number),
        )
        entry_id = _returned_id(cursor.fetchone())
        osmdb.commit()
    return Response(json.dumps({"id": entry_id}), status=201, mimetype="application/json")


def _returned_id(row: tuple[Any, ...] | None) -> int:
    """The id a RETURNING clause handed back — an INSERT always returns one."""
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id returned no row")
    return int(row[0])


@brands_bp.route("/brands/<brand_wikidata>/upload", methods=["POST"])
@auth_required
def upload_changes(brand_wikidata: str) -> ResponseReturnValue:
    batch = get_batch(brand_wikidata)
    changes, wave = batch.changes, batch.wave
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
    delete_memoized(brand_matches, brand_wikidata, wave.number)

    error_messages = [msg for _, msg in errors]
    status = determine_import_status(bulk_upload.results)
    stats = get_stats(bulk_upload.uploaded_changes)

    osmdb = get_osmdb()
    with osmdb.cursor() as cursor:
        # changeset_ids is no longer filled: the per-subdivision detail now
        # lives in import_subdivisions.
        cursor.execute(
            """INSERT INTO import_history (brand_wikidata, osm_user_id, status, comment, items_count, brand_name, tags_count, wave,
                    included_categories, excluded_categories)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                brand_wikidata,
                session["user"]["osm_id"],
                status,
                "; ".join(error_messages) or None,
                len(bulk_upload.uploaded_changes),
                bulk_upload.brand_name,
                json.dumps(stats["by_tag"]),
                wave.number,
                sorted({category_key(change) for change in bulk_upload.uploaded_changes}),
                sorted(batch.excluded),
            ),
        )
        entry_id = _returned_id(cursor.fetchone())
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
        return Response(json.dumps({"id": entry_id}), status=200, mimetype="application/json")
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
