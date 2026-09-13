import os
import time
import logging
import requests

from datetime import datetime, timedelta, timezone
from pathlib import Path


logger = logging.getLogger(__name__)


def delete_file_if_exists(file_path):
    """
    Delete a file if it exists.
    """
    if os.path.exists(file_path):
        os.remove(file_path)



def clean_debug_folder():
    for file_path in os.listdir("./data/debug"):
        os.remove(f"./data/debug/{file_path}")


print


def download_large_file(
    url: str,
    destination: str | Path,
    chunk_size: int = 8192,
    progress_interval: int = 15,
    session: requests.Session | None = None,
) -> None:
    """
    Stream a file from *url* to *destination* while printing a progress
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
            total_bytes = (
                int(total_bytes) if total_bytes and total_bytes.isdigit() else None
            )

            # If we don’t know the size we’ll fall back to a simple byte counter.
            show_percent = total_bytes is not None

            written = 0
            start = last_report = time.time()

            with open(dest_path, "wb") as out_file:
                for chunk in resp.iter_content(chunk_size=chunk_size):
                    if not chunk:  # skip keep‑alive chunks
                        continue
                    out_file.write(chunk)
                    written += len(chunk)

                    now = time.time()
                    if now - last_report >= progress_interval:
                        elapsed = now - start
                        speed = written / elapsed if elapsed > 0 else 0

                        if show_percent:
                            pct = (written / total_bytes) * 100
                            logger.info(
                                f"[{elapsed:6.1f}s] "
                                f"{pct:5.1f}% ({written:,} / {total_bytes:,} bytes) "
                                f"@ {speed / 1024:,.1f} KiB/s"
                            )
                        else:
                            # No length header → just show bytes transferred.
                            logger.info(
                                f"[{elapsed:6.1f}s] "
                                f"{written:,} bytes downloaded "
                                f"@ {speed / 1024:,.1f} KiB/s"
                            )
                        last_report = now

            # ----- final summary -------------------------------------------------
            if written == 0:
                dest_path.unlink(missing_ok=True)
                raise ValueError(f"Downloaded file is empty (0 bytes): {url}")

            total_elapsed = time.time() - start
            avg_speed = written / total_elapsed if total_elapsed > 0 else 0
            if show_percent:
                logger.info(
                    f"\nDownload complete: 100.0% ({written:,} / {total_bytes:,} bytes) "
                    f"in {total_elapsed:.1f}s ({avg_speed / 1024:,.1f} KiB/s)."
                )
            else:
                logger.info(
                    f"\nDownload complete: {written:,} bytes in "
                    f"{total_elapsed:.1f}s ({avg_speed / 1024:,.1f} KiB/s)."
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
TODO_NOT_IN_ATP_SQL = """NOT EXISTS (
    SELECT 1 FROM atp_places a
    WHERE a.brand_wikidata = todo_brands.brand_wikidata
       OR LOWER(a.brand) = LOWER(todo_brands.brand_name)
)"""


def hide_brands_in_atp(where, args, active=None):
    """Append the "not in ATP" clause unless ?show_in_atp=1 asks for them."""
    if args.get("show_in_atp"):
        if active is not None:
            active["show_in_atp"] = "1"
        return where
    return (f"{where} AND " if where else "WHERE ") + TODO_NOT_IN_ATP_SQL


def build_filters(args, spec):
    """Build a SQL WHERE clause from the query string.

    `spec` declares which filters the page exposes, and on which columns:

        {"q":      ("brand_name", "brand_wikidata"),  # ILIKE search
         "status": "status",                          # one of IMPORT_STATUSES
         "user":   "osm_user_id",                     # integer equality
         "date":   "import_date"}                     # ?from= and ?to= bounds

    A filter missing from `spec` is ignored even when present in the URL. Column
    names always come from the code, never from the request.

    Returns (where_sql, params, active_filters).
    """
    where, params, active = [], [], {}

    if "q" in spec:
        q = args.get("q", "").strip()
        if q:
            columns = spec["q"]
            where.append("(" + " OR ".join(f"{c} ILIKE %s" for c in columns) + ")")
            params += [f"%{q}%"] * len(columns)
            active["q"] = q

    if "status" in spec:
        status = args.get("status", "")
        if status in IMPORT_STATUSES:
            where.append(f"{spec['status']} = %s")
            params.append(status)
            active["status"] = status

    if "user" in spec:
        user = args.get("user", type=int)
        if user:
            where.append(f"{spec['user']} = %s")
            params.append(user)
            active["user"] = user

    if "date" in spec:
        column = spec["date"]
        date_from = args.get("from", "").strip()
        if date_from:
            where.append(f"{column} >= %s")
            params.append(date_from)
            active["from"] = date_from

        date_to = args.get("to", "").strip()
        if date_to:
            # inclusive bound: everything dated on the given day
            where.append(f"{column} < %s::date + 1")
            params.append(date_to)
            active["to"] = date_to

    return ("WHERE " + " AND ".join(where) if where else ""), params, active


def filter_brands(rows, args, search=("brand", "brand_wikidata")):
    """Filter the brand list from the query string.

    Counterpart of build_filters() for an already in-memory list — see the comment
    in the /brands view for why. The only filter proper to this list is the
    'never imported' status. `search` names the columns ?q= looks into; the
    status and the period always read last_status and last_import.

    Returns (filtered_rows, active_filters).
    """
    active = {}

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
        rows = [
            r for r in rows if r["last_import"] and str(r["last_import"].date()) >= date_from
        ]
        active["from"] = date_from

    date_to = args.get("to", "").strip()
    if date_to:
        rows = [
            r for r in rows if r["last_import"] and str(r["last_import"].date()) <= date_to
        ]
        active["to"] = date_to

    return rows, active


# ponytail: per-process in-memory cache (OSM user id -> expiry). A display_name
# almost never changes, a week is enough. With several workers each keeps its
# own — that is intended, no need to bring in Redis.
OSM_USER_CACHE_TTL = timedelta(weeks=1)
_osm_user_cache: dict[int, tuple[str, datetime]] = {}


def fetch_osm_users(user_ids):
    """Batch fetch user display names from the OSM API, cached one week."""
    from src.config import get_settings
    if not user_ids:
        return {}

    now = datetime.now(timezone.utc)
    cached = {}
    missing = []
    for uid in user_ids:
        entry = _osm_user_cache.get(uid)
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
        fetched = {
            u["user"]["id"]: u["user"]["display_name"]
            for u in resp.json().get("users", [])
        }
    except Exception:
        logger.exception("Failed to fetch OSM user details")
        return cached  # the API said nothing: serve at least what we hold

    expires = now + OSM_USER_CACHE_TTL
    for uid, name in fetched.items():
        _osm_user_cache[uid] = (name, expires)
    return cached | fetched


def _determine_import_status(results: list[dict]) -> str:
    """Derive the import_history status from its subdivision rows.

    All succeeded → success ; none → error ; a mix → partial. The error kind
    (OSM API or unexpected) stays on the subdivision row.
    """
    statuses = {r["status"] for r in results}
    if statuses <= {"success"}:
        return "success"
    return "partial" if "success" in statuses else "error"
