import logging
import time
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from operator import itemgetter
from pathlib import Path
from typing import Any, LiteralString

import requests
from psycopg import sql
from werkzeug.datastructures import MultiDict

from src.config import get_settings

logger = logging.getLogger(__name__)


def delete_file_if_exists(file_path: str | Path) -> None:
    """Delete a file if it exists."""
    Path(file_path).unlink(missing_ok=True)


def download_large_file(
    url: str,
    destination: str | Path,
    chunk_size: int = 8192,
    progress_interval: int = 15,
    session: requests.Session | None = None,
) -> None:
    """Stream a file from *url* to *destination* while printing a progress
    percentage roughly every ``progress_interval`` seconds.

    Parameters
    ----------
    url               : URL of the file to download.
    destination       : Local path where the file will be saved.
    chunk_size        : Number of bytes read per iteration (default 8192).
    progress_interval : Seconds between progress updates (default 15 s).
    """
    dest_path = Path(destination)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # ``stream=True`` gives us an iterator over the response body.
        # (connect, read): Geofabrik keeps the socket open but idle when loaded;
        # 30 s of read timeout was enough to kill a multi-GB download.
        with (session or requests).get(url, stream=True, timeout=(10, 120)) as resp:
            resp.raise_for_status()

            # Try to obtain the total size from the HTTP header.
            total_bytes = resp.headers.get("Content-Length")
            total_bytes = int(total_bytes) if total_bytes and total_bytes.isdigit() else None

            written = 0
            start = last_report = time.time()

            with dest_path.open("wb") as out_file:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if not chunk:  # skip keep‑alive chunks
                        continue
                    out_file.write(chunk)
                    written += len(chunk)

                    now = time.time()
                    if now - last_report >= progress_interval:
                        elapsed = now - start
                        speed = written / elapsed if elapsed > 0 else 0

                        if total_bytes is not None:
                            logger.info(
                                "[%6.1fs] %5.1f%% (%s / %s bytes) @ %s KiB/s",
                                elapsed,
                                written / total_bytes * 100,
                                f"{written:,}",
                                f"{total_bytes:,}",
                                f"{speed / 1024:,.1f}",
                            )
                        else:
                            # No length header → just show bytes transferred.
                            logger.info(
                                "[%6.1fs] %s bytes downloaded @ %s KiB/s",
                                elapsed,
                                f"{written:,}",
                                f"{speed / 1024:,.1f}",
                            )
                        last_report = now

            # ----- final summary -------------------------------------------------
            if written == 0:
                dest_path.unlink(missing_ok=True)
                raise ValueError(f"Downloaded file is empty (0 bytes): {url}")

            total_elapsed = time.time() - start
            avg_speed = written / total_elapsed if total_elapsed > 0 else 0
            logger.info(
                "Download complete: %s bytes in %.1fs (%s KiB/s).",
                f"{written:,}",
                total_elapsed,
                f"{avg_speed / 1024:,.1f}",
            )

    except requests.exceptions.RequestException:
        dest_path.unlink(missing_ok=True)
        raise


# The fate of an integration. The error type lives one level below, on the
# subdivision row: that is where it means something.
IMPORT_STATUSES = ("success", "partial", "cancelled", "error")


def status_group(status: str | None) -> str:
    """The status itself, or 'none' for a brand never integrated."""
    return status if status in IMPORT_STATUSES else "none"


# The filters offered by the history and missing-brands pages, and the columns
# they apply to. Defined here because a page and its export must offer exactly
# the same filters.
HISTORY_FILTERS = {
    "q": ("brand_name", "brand_wikidata"),
    "status": "status",
    "wave": "wave",
    "user": "osm_user_id",
    "date": "import_date",
}

# No status here: a brand still to integrate has none.
TODO_FILTERS = {
    "q": ("brand_name", "brand_wikidata"),
    "user": "osm_user_id",
    "date": "created_at",
}


# The missing-brands list hides by default the ones ATP already knows: that is
# its whole point. ?show_in_atp=1 shows them again. Shared between the page and
# its export, which must return the same rows.
TODO_NOT_IN_ATP_SQL = sql.SQL("""NOT EXISTS (
    SELECT 1 FROM atp_places a
    WHERE a.brand_wikidata = todo_brands.brand_wikidata
       OR LOWER(a.brand) = LOWER(todo_brands.brand_name)
)""")

# What the query string turns into: the conditions of a WHERE clause, their
# bound parameters, and the filters honoured, for the page to show them.
FilterSpec = Mapping[str, str | tuple[str, ...]]
Filters = dict[str, str | int]
Params = list[str | int]
Conditions = list[sql.Composable]


def where_clause(conditions: Conditions, lead: LiteralString = "WHERE") -> sql.Composable:
    """The WHERE clause joining `conditions`, empty when there are none.

    `lead` is what opens it — "AND" when the conditions extend a JOIN's ON.
    """
    if not conditions:
        return sql.SQL("")
    return sql.SQL(lead) + sql.SQL(" ") + sql.SQL(" AND ").join(conditions)


def hide_brands_in_atp(
    conditions: Conditions, args: MultiDict[str, str], active: Filters | None = None
) -> Conditions:
    """Add the "not in ATP" condition unless ?show_in_atp=1 asks for them."""
    if args.get("show_in_atp"):
        if active is not None:
            active["show_in_atp"] = "1"
        return conditions
    return [*conditions, TODO_NOT_IN_ATP_SQL]


def _iso_date(value: str) -> str:
    """The value if it is a YYYY-MM-DD date, else empty."""
    value = value.strip()
    try:
        date.fromisoformat(value)
    except ValueError:
        return ""
    return value


def _column(name: str, alias: str | None) -> sql.Identifier:
    return sql.Identifier(alias, name) if alias else sql.Identifier(name)


def _one_column(spec: FilterSpec, key: str, alias: str | None) -> sql.Identifier:
    name = spec[key]
    if not isinstance(name, str):
        raise TypeError(f"filter '{key}' names one column, got {name!r}")
    return _column(name, alias)


def _search_condition(
    columns: str | tuple[str, ...], needle: str, alias: str | None
) -> tuple[sql.Composable, Params]:
    columns = (columns,) if isinstance(columns, str) else columns
    condition = (
        sql.SQL("(")
        + sql.SQL(" OR ").join(sql.SQL("{} ILIKE %s").format(_column(c, alias)) for c in columns)
        + sql.SQL(")")
    )
    return condition, [f"%{needle}%"] * len(columns)


def build_filters(
    args: MultiDict[str, str], spec: FilterSpec, alias: str | None = None
) -> tuple[Conditions, Params, Filters]:
    """Build the conditions of a SQL WHERE clause from the query string.

    `spec` declares which filters the page exposes, and on which columns:

        {"q":      ("brand_name", "brand_wikidata"),  # ILIKE search
         "status": "status",                          # one of IMPORT_STATUSES
         "wave":   "wave",                            # integer equality
         "user":   "osm_user_id",                     # integer equality
         "date":   "import_date"}                     # ?from= and ?to= bounds

    A filter missing from `spec` is ignored even when present in the URL. Column
    names always come from the code, never from the request; `alias` qualifies
    them, for a query that names the table.

    Returns (conditions, params, active_filters) — see where_clause().
    """
    conditions: Conditions = []
    params: Params = []
    active: Filters = {}

    if "q" in spec:
        q = args.get("q", "").strip()
        if q:
            condition, bound = _search_condition(spec["q"], q, alias)
            conditions.append(condition)
            params += bound
            active["q"] = q

    if "status" in spec:
        status = args.get("status", "")
        if status in IMPORT_STATUSES:
            conditions.append(sql.SQL("{} = %s").format(_one_column(spec, "status", alias)))
            params.append(status)
            active["status"] = status

    for key in ("wave", "user"):
        if key in spec:
            value = args.get(key, type=int)
            if value:
                conditions.append(sql.SQL("{} = %s").format(_one_column(spec, key, alias)))
                params.append(value)
                active[key] = value

    if "date" in spec:
        # A value that is not a date is ignored, like an unknown status: the
        # query never sees it, so the database never refuses it.
        date_from = _iso_date(args.get("from", ""))
        if date_from:
            conditions.append(sql.SQL("{} >= %s").format(_one_column(spec, "date", alias)))
            params.append(date_from)
            active["from"] = date_from

        date_to = _iso_date(args.get("to", ""))
        if date_to:
            # inclusive bound: everything dated on the given day
            conditions.append(sql.SQL("{} < %s::date + 1").format(_one_column(spec, "date", alias)))
            params.append(date_to)
            active["to"] = date_to

    return conditions, params, active


def filter_brands(
    rows: Sequence[Mapping[str, Any]],
    args: MultiDict[str, str],
    search: tuple[str, ...] = ("brand", "brand_wikidata"),
) -> tuple[list[Mapping[str, Any]], Filters]:
    """Filter the brand list from the query string.

    Counterpart of build_filters() for an already in-memory list — see the comment
    in the /brands view for why. The only filter proper to this list is the
    'never imported' status. `search` names the columns ?q= looks into; the
    status and the period always read last_status and last_import.

    Returns (filtered_rows, active_filters).
    """
    active: Filters = {}
    rows = list(rows)

    q = args.get("q", "").strip()
    if q:
        needle = q.lower()
        rows = [r for r in rows if any(needle in (r[c] or "").lower() for c in search)]
        active["q"] = q

    status = args.get("status", "")
    if status:
        rows = [r for r in rows if status_group(r["last_status"]) == status]
        active["status"] = status

    wave = args.get("wave", "")
    if wave.isdigit():
        rows = [r for r in rows if r["wave"] == int(wave)]
        active["wave"] = int(wave)

    date_from = args.get("from", "").strip()
    if date_from:
        rows = [r for r in rows if r["last_import"] and str(r["last_import"].date()) >= date_from]
        active["from"] = date_from

    date_to = args.get("to", "").strip()
    if date_to:
        rows = [r for r in rows if r["last_import"] and str(r["last_import"].date()) <= date_to]
        active["to"] = date_to

    return rows, active


# The columns a table is sorted on, the leading one first, each with whether
# it runs descending. The query string carries them as ?sort=&dir= pairs,
# repeated: a shift+click on a header appends one.
Sorts = list[tuple[str, bool]]


def parse_sorts(args: MultiDict[str, str], columns: Mapping[str, str], default: Sorts) -> Sorts:
    """The sorts of the query string on `columns`, or `default` if none holds.

    An unknown column is dropped, a column named twice keeps its first
    place, a missing dir is descending.
    """
    dirs = args.getlist("dir")
    sorts: dict[str, bool] = {}
    for i, key in enumerate(args.getlist("sort")):
        if key in columns and key not in sorts:
            sorts[key] = (dirs[i] if i < len(dirs) else "desc") != "asc"
    return list(sorts.items()) or default


def order_by(sorts: Sorts, columns: Mapping[str, str]) -> sql.Composable:
    """The ORDER BY list of `sorts`, empty values last whichever the direction."""
    return sql.SQL(", ").join(
        sql.SQL("{} {} NULLS LAST").format(
            sql.Identifier(columns[key]), sql.SQL("DESC" if descending else "ASC")
        )
        for key, descending in sorts
    )


def sort_rows(
    rows: Iterable[Mapping[str, Any]], sorts: Sorts, columns: Mapping[str, str]
) -> list[Mapping[str, Any]]:
    """In-memory counterpart of order_by(): empty values last in both directions.

    A column the reader sorts is one they want to see filled: a descending
    sort that opened on a page of blanks would look like it did nothing.
    The sort is stable, so sorting on the last column first leaves each
    earlier one breaking the ties of the next.
    """
    rows = list(rows)
    for key, descending in reversed(sorts):
        column = columns[key]
        filled = [r for r in rows if r[column] is not None]
        empty = [r for r in rows if r[column] is None]
        rows = sorted(filled, key=itemgetter(column), reverse=descending) + empty
    return rows


# Per-process in-memory cache (OSM user id -> expiry). A display_name almost
# never changes, so a week is enough, and each worker keeping its own copy
# costs at most one extra API call per worker.
OSM_USER_CACHE_TTL = timedelta(weeks=1)
osm_user_cache: dict[int, tuple[str, datetime]] = {}


def fetch_osm_users(user_ids: Iterable[int]) -> dict[int, str]:
    """Batch fetch user display names from the OSM API, cached one week."""
    if not user_ids:
        return {}

    now = datetime.now(UTC)
    cached: dict[int, str] = {}
    missing: list[int] = []
    for uid in user_ids:
        entry = osm_user_cache.get(uid)
        if entry and entry[1] > now:
            cached[uid] = entry[0]
        else:
            missing.append(uid)
    if not missing:
        return cached

    settings = get_settings()
    ids_param = ",".join(str(uid) for uid in missing)
    try:
        resp = requests.get(
            f"{settings.api_url}/api/0.6/users.json?users={ids_param}",
            timeout=5,
            headers={"User-Agent": f"atp2osm/{settings.app_version}"},
        )
        resp.raise_for_status()
        users: list[dict[str, dict[str, Any]]] = resp.json().get("users", [])
        fetched = {int(u["user"]["id"]): str(u["user"]["display_name"]) for u in users}
    except Exception:
        logger.exception("Failed to fetch OSM user details")
        return cached  # the API said nothing: serve at least what we hold

    expires = now + OSM_USER_CACHE_TTL
    for uid, name in fetched.items():
        osm_user_cache[uid] = (name, expires)
    return cached | fetched


def determine_import_status(results: Iterable[Mapping[str, Any]]) -> str:
    """Derive the import_history status from its subdivision rows.

    All succeeded → success ; none → error ; a mix → partial. The error kind
    (OSM API or unexpected) stays on the subdivision row.
    """
    statuses = {r["status"] for r in results}
    if statuses <= {"success"}:
        return "success"
    return "partial" if "success" in statuses else "error"
