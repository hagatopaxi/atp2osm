"""Never overwrite a value a human posted recently.

ATP proposes tags read off the brands' own websites. When a mapper filled in
an `opening_hours` or a `phone` by hand last week, that value is a deliberate
choice, often surveyed on the ground: it wins over ours. A value a bot posted
was verified by nobody and can be replaced.

The protection is **per tag**, not per object: a POI whose `name` was fixed
yesterday still takes an ATP `website`.

Two filters do all the saving. `osm_timestamp`, which the PBF already gives
us, drops the large majority of POIs without a single request; and only the
tags the diff wants to write are ever dated — dating the rest would buy
nothing.
"""

import logging
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import requests

from src.config import get_settings
from src.matching import changed_tags

logger = logging.getLogger(__name__)

TIMEOUT = (5, 15)


def _headers() -> dict:
    settings = get_settings()
    return {"User-Agent": f"atp2osm/{settings.app_version}"}


def _threshold() -> datetime:
    return datetime.now(timezone.utc) - timedelta(
        weeks=get_settings().recent_edit_weeks
    )


def _parse(value) -> datetime | None:
    """An OSM timestamp, or one of ours, as an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _get(path: str) -> dict | None:
    """One read off the OSM API. Reasonable use: sequential, identified."""
    url = f"{get_settings().api_url}/api/0.6/{path}.json"
    try:
        response = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError):
        logger.warning("OSM API read failed: %s", url, exc_info=True)
        return None


def versions(node_type: str, osm_id: int) -> list[dict]:
    """Every version of an object, oldest first."""
    payload = _get(f"{node_type}/{osm_id}/history")
    elements = (payload or {}).get("elements") or []
    return sorted(elements, key=lambda v: v.get("version", 0))


@lru_cache(maxsize=4096)
def is_bot(changeset_id: int) -> bool:
    """`bot=yes` on the changeset — the only marker retained.

    The user name is no signal: OSM imposes no naming convention and plenty of
    imports run under an ordinary account. `created_by` names the tool, not the
    nature of the edit. A bot that does not declare itself is therefore treated
    as a human and its value is preserved: the doubt benefits what is there.
    """
    payload = _get(f"changeset/{changeset_id}")
    elements = (payload or {}).get("elements") or []
    if not elements:
        return False
    return (elements[0].get("tags") or {}).get("bot") == "yes"


def value_set_at(history: list[dict], key: str) -> tuple[datetime | None, int | None]:
    """When the current value of *key* was posted, and by which changeset.

    Walking down from the current version, the answer is the oldest consecutive
    version carrying the current value — that is the one that posted it.
    Absence is a value like any other in that comparison: a version rewriting
    the same value does not break the run, and a value recreated identically
    after a deletion does break it. A single-version object answers v1.
    """
    if not history:
        return None, None
    current = (history[-1].get("tags") or {}).get(key)
    setter = history[-1]
    for version in reversed(history[:-1]):
        if (version.get("tags") or {}).get(key) != current:
            break
        setter = version
    return _parse(setter.get("timestamp")), setter.get("changeset")


def _replaced_tags(change: dict) -> set[str]:
    """Keys the diff would overwrite — the only ones the protection is about.

    A wave that only adds tags produces none, and then costs no request.
    """
    old = change.get("old_tag") or {}
    return {key for key in changed_tags(change) if key in old}


def protect_recent_edits(changes: list[dict]) -> list[dict]:
    """Strip from *changes* every tag a human wrote recently.

    Returns the changes worth uploading: one whose every tag was protected
    drops out, since it would be an empty changeset.
    """
    threshold = _threshold()
    kept = []

    for change in changes:
        replaced = _replaced_tags(change)
        edited = _parse(change.get("osm_timestamp"))
        # The majority case, and it costs nothing: the object as a whole has
        # not moved since the threshold, so no tag on it can have.
        if replaced and (edited is None or edited >= threshold):
            history = versions(change["node_type"], change["id"])
            for key in sorted(replaced):
                when, changeset = value_set_at(history, key)
                if when is not None and when < threshold:
                    continue
                if changeset is not None and is_bot(changeset):
                    continue
                logger.info(
                    "%s/%s: keeping the recent %s",
                    change["node_type"], change["id"], key,
                )
                change["tag"][key] = change["old_tag"][key]

        if change["tag"] != change["old_tag"]:
            kept.append(change)

    return kept
