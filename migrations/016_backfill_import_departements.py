"""Backfill: rebuild import_departements from the logs.

Every integration left a `logs/<brand_wikidata>/<date>.json` file holding two
concatenated JSON arrays:

  1. the submitted POIs — with their département, their before/after tags and
     the `changeset` key written at upload time (so including a département
     whose upload later failed);
  2. the ids of the *successful* changesets.

Crossing the two gives, département by département, the count, the attempted
changeset and its fate — exactly one import_departements row. The before/after
tags give tags_count along the way, and the number of POIs actually uploaded
gives items_count.

The success, partial_* and error_* cases are all covered:
  - a département whose changeset appears in array 2 → success;
  - otherwise → error_osm_api / error_unknown, the suffix taken from the
    integration status (the two levels cannot diverge: the suffix already
    comes from the same errors).

A few logs did not keep the `changeset` key on the POIs: since the successful
ids were recorded in département order, they are handed back in that order.

Not backfilled: cancellations (`cancelled`, no changeset attempted), empty
integrations, those whose log file is missing, and the very first logs, older
than the recording of the département. The latter keep a trace of their
changesets in their comment, for want of anything better.
"""

import json
import logging
import os
import pathlib
import re
from datetime import timedelta
from typing import Any

from psycopg.rows import dict_row

from src.matching import get_stats
from src.migrate import Migration

logger = logging.getLogger(__name__)

# A row of import_history, a change as the log holds it, a child row to write.
Row = dict[str, Any]
LoggedChange = dict[str, Any]
Child = dict[str, Any]

LOGS_DIR = pathlib.Path(
    os.environ.get("ATP2OSM_LOGS_DIR", pathlib.Path(__file__).parent.parent / "logs")
)

# "OSM API error for dept 94: ..." / "Unknown error for dept 94: ..." — the
# integration comment names every failed département and says which error it
# was.
FAILED_DPT_RE = re.compile(r"(OSM API|Unknown) error for dept (\w+):")
ERROR_KINDS = {"OSM API": "error_osm_api", "Unknown": "error_unknown"}


class BackfillImportDepartements(Migration):
    def migrate(self) -> None:
        with self.conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute(
                """SELECT id, brand_wikidata, brand_name, import_date, status, comment,
                          items_count, tags_count, changeset_ids
                   FROM import_history
                   WHERE status <> 'cancelled'
                     AND (items_count IS NULL OR items_count > 0)
                     AND NOT EXISTS (
                         SELECT 1 FROM import_departements dpt WHERE dpt.import_id = import_history.id
                     )
                   ORDER BY import_date"""
            )
            rows: list[Row] = cursor.fetchall()

        # One log file per (folder, day): two integrations landing on the same
        # file overwrote each other. Since the rows are sorted by date, the last
        # one is what the file describes; the earlier ones stay without detail.
        paths = {row["id"]: self._find_log_path(row) for row in rows}
        claimed: dict[pathlib.Path, int] = {}
        for row in rows:
            path = paths[row["id"]]
            if path is not None:
                claimed[path] = row["id"]

        done = 0
        for row in rows:
            path = paths[row["id"]]
            if path is None:
                logger.warning(
                    "No log for integration %s (%s, %s): left without detail.",
                    row["id"],
                    row["brand_wikidata"],
                    row["import_date"].date(),
                )
                self._keep_changeset_ids(row)
                continue

            if claimed[path] != row["id"]:
                logger.warning(
                    "Import %s not backfilled: log %s describes integration %s",
                    row["id"],
                    path,
                    claimed[path],
                )
                self._keep_changeset_ids(row)
                continue

            changes, succeeded = self._read_log(path)

            # The oldest logs recorded neither the département nor the
            # changeset: nothing to split, the integration stays without
            # detail.
            if not changes or any("departement_number" not in c for c in changes):
                logger.warning(
                    "Log %s in the old format (no département): "
                    "integration %s left without detail.",
                    path,
                    row["id"],
                )
                self._keep_changeset_ids(row)
                continue

            self._write(row, changes, succeeded)
            done += 1

        logger.info("Backfill done: %d integration(s) detailed.", done)

    def _find_log_path(self, row: Row) -> pathlib.Path | None:
        """The log file of an integration, or None.

        The file name comes from the server's local time while the integration
        date is in UTC: the day before and the day after are tried too.

        The folder carries the *OSM* brand:wikidata of the first POI, which is
        not always the ATP brand's (`Q246/` for brand `Q699709`), and is
        `unknown` when that tag is missing. Failing the expected folder, we
        therefore look for the day's log whose POIs carry the brand name.
        """
        day = row["import_date"].date()
        for date in (day, day - timedelta(days=1), day + timedelta(days=1)):
            expected = LOGS_DIR / row["brand_wikidata"] / f"{date}.json"
            if expected.is_file():
                return expected
            for path in sorted(LOGS_DIR.glob(f"*/{date}.json")):
                changes, _ = self._read_log(path)
                if changes and changes[0].get("atp_brand") == row["brand_name"]:
                    logger.info(
                        "Integration %s (%s) found back in %s, matched by name.",
                        row["id"],
                        row["brand_wikidata"],
                        path.parent.name,
                    )
                    return path
        return None

    def _keep_changeset_ids(self, row: Row) -> None:
        """changeset_ids disappears right after (migration 017): for an
        integration we cannot detail, at least keep a trace of its changesets
        in the comment.
        """
        if not row["changeset_ids"]:
            return
        trace = "Changesets : " + ", ".join(str(c) for c in row["changeset_ids"])
        with self.conn.cursor() as cursor:
            cursor.execute(
                "UPDATE import_history SET comment = CONCAT_WS(' — ', comment, %s::text) WHERE id = %s",
                (trace, row["id"]),
            )

    def _read_log(self, path: pathlib.Path) -> tuple[list[LoggedChange], list[int] | None]:
        """Return (changes, succeeded); succeeded is None when the log predates
        the recording of successful changesets.
        """
        decoder = json.JSONDecoder()
        text = path.read_text(encoding="utf-8")
        changes, end = decoder.raw_decode(text)
        rest = text[end:].strip()
        succeeded = decoder.raw_decode(rest)[0] if rest else None
        return changes, succeeded

    def _write(self, row: Row, changes: list[LoggedChange], succeeded: list[int] | None) -> None:
        children, uploaded = reconstruct(changes, succeeded, row["status"], row["comment"])
        rows = [
            (
                row["id"],
                c["departement_number"],
                c["items_count"],
                c["osm_changeset_id"],
                c["status"],
                c["comment"],
            )
            for c in children
        ]

        with self.conn.cursor() as cursor:
            cursor.executemany(
                """INSERT INTO import_departements
                       (import_id, departement_number, items_count, osm_changeset_id, status, comment)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                rows,
            )
            # The columns the integration could not fill at the time.
            cursor.execute(
                """UPDATE import_history
                   SET items_count = COALESCE(items_count, %s),
                       tags_count  = COALESCE(tags_count, %s)
                   WHERE id = %s""",
                (len(uploaded), json.dumps(get_stats(uploaded)["by_tag"]), row["id"]),
            )


def reconstruct(
    changes: list[LoggedChange], succeeded: list[int] | None, status: str, comment: str | None
) -> tuple[list[Child], list[LoggedChange]]:
    """Per-département detail of an integration, from its log.

    *succeeded* is the list of successful changesets, or None when the log
    predates its recording — we then fall back on the départements named in the
    error comment, which also gives the type of each failure.

    Returns (import_departements rows, POIs actually uploaded).
    """
    # Failing a comment naming names, the suffix of the integration status
    # (before migration 018, which drops it); otherwise the error stays
    # unknown.
    default_error = "error_osm_api" if status.endswith("osm_api") else "error_unknown"
    failed_from_comment = {
        _pad(dpt): ERROR_KINDS[kind] for kind, dpt in FAILED_DPT_RE.findall(comment or "")
    }

    by_dpt: dict[str, dict[str, Any]] = {}
    for change in changes:
        dpt = _pad(change["departement_number"])
        entry = by_dpt.setdefault(dpt, {"changeset": None, "changes": []})
        entry["changes"].append(change)
        entry["changeset"] = entry["changeset"] or change.get("changeset")

    # Some logs did not keep the changeset on each POI. Since the successful
    # ids were recorded in département order, they are handed back in that same
    # order to the départements the comment does not blame.
    positional: dict[str, int] | None = None
    if succeeded is not None and any(e["changeset"] is None for e in by_dpt.values()):
        candidates = [d for d in by_dpt if d not in failed_from_comment]
        if len(candidates) != len(succeeded):
            logger.warning(
                "%d candidate département(s) for %d successful changeset(s): "
                "the last ones are counted as failed.",
                len(candidates),
                len(succeeded),
            )
        positional = dict(zip(candidates, succeeded, strict=False))

    children: list[Child] = []
    uploaded: list[LoggedChange] = []
    for dpt, entry in by_dpt.items():
        if positional is not None:
            ok = dpt in positional
            changeset = positional.get(dpt)
        elif succeeded is None:
            ok = True
            changeset = entry["changeset"]
        else:
            ok = entry["changeset"] in succeeded
            changeset = entry["changeset"]

        # The integration comment names the failed départements
        # ("OSM API error for dept 94: ..."): it decides.
        if dpt in failed_from_comment:
            ok = False

        if ok:
            uploaded.extend(entry["changes"])
        else:
            # A failed département keeps its changeset when it was created:
            # only a creation failure leaves the id null. In positional mode we
            # do not know which one it was.
            changeset = entry["changeset"]

        children.append(
            {
                "departement_number": dpt,
                "items_count": len(entry["changes"]),
                "osm_changeset_id": changeset,
                "status": "success" if ok else failed_from_comment.get(dpt, default_error),
                "comment": None if ok else comment,
            }
        )

    return children, uploaded


def _pad(departement_number: str | int) -> str:
    """The oldest logs stored the département as an integer (6, 94)."""
    return str(departement_number).zfill(2)
