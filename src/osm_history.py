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
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import requests

from src.config import get_settings
from src.matching import Change, changed_tags

# One version of an object, as the OSM API returns it.
Version = dict[str, Any]

logger = logging.getLogger(__name__)

TIMEOUT = (5, 15)


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {"User-Agent": f"atp2osm/{settings.app_version}"}


def _threshold() -> datetime:
    return datetime.now(UTC) - timedelta(weeks=get_settings().recent_edit_weeks)


def parse_timestamp(value: str | datetime | None) -> datetime | None:
    """An OSM timestamp, or one of ours, as an aware datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value))


class OsmApiUnavailableError(Exception):
    """The OSM API could not answer a read the protection needs.

    Not a value: a read that fails dates nothing, and a tag that cannot be
    dated cannot be decided on. Answering "keep it" instead would let a batch
    whose every value went undated drop out whole, and /validate would then
    close the brand as integrated for the length of a cooldown — a transient
    outage turned into three months of silence. So the failure is raised, and
    the page says the API is down.
    """


def _get(path: str) -> dict[str, Any]:
    """One read off the OSM API. Reasonable use: sequential, identified."""
    url = f"{get_settings().api_url}/api/0.6/{path}.json"
    try:
        response = requests.get(url, headers=_headers(), timeout=TIMEOUT)
        response.raise_for_status()
        return response.json()
    except (requests.exceptions.RequestException, ValueError) as exc:
        logger.warning("OSM API read failed: %s", url, exc_info=True)
        raise OsmApiUnavailableError(f"{url}: {exc}") from exc


def versions(node_type: str, osm_id: int) -> list[Version]:
    """Every version of an object, oldest first."""
    elements: list[Version] = _get(f"{node_type}/{osm_id}/history").get("elements") or []
    return sorted(elements, key=lambda v: int(v.get("version", 0)))


@lru_cache(maxsize=4096)
def is_bot(changeset_id: int) -> bool:
    """`bot=yes` on the changeset — the only marker retained.

    The user name is no signal: OSM imposes no naming convention and plenty of
    imports run under an ordinary account. `created_by` names the tool, not the
    nature of the edit. A bot that does not declare itself is therefore treated
    as a human and its value is preserved: the doubt benefits what is there.
    """
    elements: list[Version] = _get(f"changeset/{changeset_id}").get("elements") or []
    if not elements:
        return False
    tags: dict[str, str] = elements[0].get("tags") or {}
    return tags.get("bot") == "yes"


def _tags(version: Version) -> dict[str, str]:
    return version.get("tags") or {}


def value_set_at(history: list[Version], key: str) -> tuple[datetime | None, int | None]:
    """When the current value of *key* was posted, and by which changeset.

    Walking down from the current version, the answer is the oldest consecutive
    version carrying the current value — that is the one that posted it.
    Absence is a value like any other in that comparison: a version rewriting
    the same value does not break the run, and a value recreated identically
    after a deletion does break it. A single-version object answers v1.
    """
    if not history:
        return None, None
    current = _tags(history[-1]).get(key)
    setter = history[-1]
    for version in reversed(history[:-1]):
        if _tags(version).get(key) != current:
            break
        setter = version
    changeset = setter.get("changeset")
    return parse_timestamp(setter.get("timestamp")), int(
        changeset
    ) if changeset is not None else None


def _replaced_tags(change: Change) -> set[str]:
    """Keys the diff would overwrite — the only ones the protection is about.

    A wave that only adds tags produces none, and then costs no request.
    """
    return {key for key in changed_tags(change) if key in change["old_tag"]}


def protect_recent_edits(changes: list[Change]) -> list[Change]:
    """Strip from *changes* every tag a human wrote recently.

    Returns the changes worth uploading: one whose every tag was protected
    drops out, since it would be an empty changeset.

    Raises OsmApiUnavailableError when a date could not be read: the caller must
    not take an undecided batch for an empty one.
    """
    # The development API server holds none of the production objects: every
    # history read is a 404 there, and the guard would refuse every batch.
    # Same as the fake OSM API the upload takes in development: nothing is
    # protected, nothing is asked.
    if get_settings().is_dev:
        return list(changes)

    threshold = _threshold()
    kept: list[Change] = []

    for change in changes:
        replaced = _replaced_tags(change)
        edited = parse_timestamp(change["osm_timestamp"])
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
                    change["node_type"],
                    change["id"],
                    key,
                )
                change["tag"][key] = change["old_tag"][key]

        if change["tag"] != change["old_tag"]:
            kept.append(change)

    return kept
