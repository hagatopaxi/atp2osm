import datetime
import json
import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol, TypedDict

import osmapi
from flask_babel import gettext
from osmapi.errors import ApiError
from requests_oauthlib import OAuth2Session

from src.config import get_settings
from src.matching import BATCH_MAX_SIZE, Change, changed_tags, subdivision_names

logger = logging.getLogger(__name__)

# An element as osmapi takes it: id, version, changeset, tag, and nd or member.
Element = dict[str, Any]
OscWriter = Callable[[int, str, int, str, Element], None]


class OsmApi(Protocol):
    """What the upload asks of the OSM API — osmapi's surface, and the fake's."""

    # osmapi keeps the open changeset there, and only there: the upload reads
    # it to close what an exception left open.
    _current_changeset_id: int

    def changeset_create(self, changeset_tags: dict[str, str] | None = None) -> int: ...
    def changeset_upload(self, changes_data: list[Element]) -> list[Element]: ...
    def way_update(self, way_data: Element) -> Element | None: ...
    def relation_update(self, relation_data: Element) -> Element | None: ...
    def changeset_close(self) -> int: ...


class SubdivisionResult(TypedDict):
    """The fate of one subdivision's changeset — a row of import_subdivisions."""

    subdivision_code: str
    subdivision_name: str | None
    items_count: int
    tag_counts: dict[str, int]
    osm_changeset_id: int | None
    status: str
    comment: str | None


class FakeOsmApi:
    """Stands in for the OSM API in development.

    The dev instance does not mirror production data, so node, way and
    relation lookups answer 404 and no upload can succeed against it. This
    records the calls instead of sending them, and writes the OSC of each one
    so the generated content can be read. It is also what the tests drive:
    the single code path of `upload` is then the one production runs.
    """

    def __init__(self, osc_writer: OscWriter | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.osc_writer = osc_writer
        self._current_changeset_id = 0
        self._next_id = 0

    def changeset_create(self, changeset_tags: dict[str, str] | None = None) -> int:
        self._next_id += 1
        self._current_changeset_id = self._next_id
        self.calls.append(("changeset_create", changeset_tags))
        return self._current_changeset_id

    def changeset_upload(self, changes_data: list[Element]) -> list[Element]:
        self.calls.append(("changeset_upload", changes_data))
        for block in changes_data:
            for data in block["data"]:
                self._osc(block["type"], data["id"], block["action"], data)
        return []

    def way_update(self, way_data: Element) -> Element | None:
        self.calls.append(("way_update", way_data))
        self._osc("way", way_data["id"], "modify", way_data)
        return None

    def relation_update(self, relation_data: Element) -> Element | None:
        self.calls.append(("relation_update", relation_data))
        self._osc("relation", relation_data["id"], "modify", relation_data)
        return None

    def changeset_close(self) -> int:
        closed = self._current_changeset_id
        self.calls.append(("changeset_close", closed))
        self._current_changeset_id = 0
        return closed

    @property
    def open_changeset(self) -> int:
        """The changeset osmapi believes open, 0 when none — what the tests read."""
        return self._current_changeset_id

    def _osc(self, element_type: str, element_id: int, action: str, data: Element) -> None:
        if self.osc_writer:
            self.osc_writer(self._current_changeset_id, element_type, element_id, action, data)


class BulkUpload:
    """Bulk uploads a changeset to the OSM server.

    Batch by subdivision and brand wikidata.
    """

    def __init__(
        self,
        changes: list[Change],
        session: OAuth2Session,
        max_size: int = BATCH_MAX_SIZE,
    ) -> None:
        # Last gate before an irreversible send, and the only one at the point
        # where changesets are actually created. select_batch already truncates
        # to that size and the route checks it again; if both were ever wrong,
        # nothing must leave for OSM. The size is the wave's: wave 2 sends one
        # POI at a time, and must not be allowed a hundred.
        if len(changes) > max_size:
            raise ValueError(f"refusing to upload {len(changes)} POIs, over the {max_size} limit")

        self.changes = changes
        # An empty batch is a no-op rather than a crash: `upload` and
        # `save_log_file` already return early on one.
        self.brand_name = changes[0]["atp_brand"] if changes else ""
        self.brand_wikidata = (
            changes[0]["tag"].get("brand:wikidata") if changes else None
        ) or "unknown"
        self.changesets: list[int] = []
        self.uploaded_changes: list[Change] = []  # POIs whose subdivision changeset succeeded
        self.results: list[SubdivisionResult] = []  # one per subdivision, see import_subdivisions

        settings = get_settings()
        self.api: OsmApi = (
            FakeOsmApi(osc_writer=self._write_osc)
            if settings.is_dev
            else osmapi.OsmApi(api=settings.api_url, session=session)
        )

    def save_log_file(self) -> Path | None:
        if len(self.changes) == 0:
            logger.info("No change in this run, no log saved.")
            return None

        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        save_path = Path(f"./logs/{self.brand_wikidata}/{today}.json")
        save_path.parent.mkdir(parents=True, exist_ok=True)

        with save_path.open("w") as file:
            json.dump(
                {"changes": self.changes, "changesets": self.changesets},
                file,
                indent=4,
                ensure_ascii=False,
            )

        logger.debug("Logs for the run saved into %s", save_path)
        return save_path

    def upload(self) -> list[tuple[str, str]]:
        """Upload all changes. Returns a list of (error_type, message) tuples; empty list means full success.
        error_type is 'osm_api' for OSM API errors (ApiError), 'unknown' for unexpected exceptions.

        The changeset comment follows the language the contributor is browsing
        in. That is a language of the country — LOCALES holds the country's
        languages — so the community that reviews the changeset can read it,
        whoever uploaded it.
        """
        if len(self.changes) == 0:
            return []

        changes_by_subdivision = self._sorted_by_subdivision()
        names = subdivision_names(self.changes)
        errors: list[tuple[str, str]] = []

        for sub, sub_changes in changes_by_subdivision.items():
            changeset: int | None = None
            try:
                sub_label = names[sub]
                changeset = self.api.changeset_create(
                    {
                        "comment": gettext(
                            "ATP data integration (%(subdivision)s; %(brand)s)",
                            subdivision=sub_label,
                            brand=self.brand_name,
                        ),
                        "created_by": "atp2osm",
                        "source": "https://alltheplaces.xyz",
                        "wiki": "https://wiki.openstreetmap.org/wiki/atp2osm",
                        "bot": "yes",
                    }
                )
                logger.debug("%s/changeset/%s", get_settings().api_url, changeset)

                changing_nodes: list[Change] = []
                for poi in sub_changes:
                    poi["changeset"] = changeset

                    if poi["node_type"] == "node":
                        changing_nodes.append(poi)
                    elif poi["node_type"] == "way":
                        self.api.way_update(
                            {
                                "id": poi["id"],
                                "version": poi["version"],
                                "changeset": changeset,
                                "tag": poi["tag"],
                                "nd": poi["members"],
                            }
                        )
                    elif poi["node_type"] == "relation":
                        type_map = {"n": "node", "w": "way", "r": "relation"}
                        relation_data = {
                            "id": poi["id"],
                            "version": poi["version"],
                            "changeset": changeset,
                            "tag": poi["tag"],
                            "member": [
                                {
                                    "type": type_map[m["type"]],
                                    "ref": m["ref"],
                                    "role": m["role"],
                                }
                                for m in (poi["members"] or [])
                            ],
                        }
                        self.api.relation_update(relation_data)

                if changing_nodes:
                    self.api.changeset_upload(
                        [{"type": "node", "action": "modify", "data": changing_nodes}]
                    )

                self.api.changeset_close()
                self.changesets.append(changeset)
                self.uploaded_changes.extend(sub_changes)
                self._record(sub, sub_changes, "success", changeset, None)
            except ApiError as error:
                msg = f"OSM API error for subdivision {sub}: HTTP {error.status} — {error.payload_str}"
                logger.exception(msg)
                errors.append(("osm_api", msg))
                # changeset is None only when its creation failed: it does
                # exist when it is the upload that gave up.
                self._record(sub, sub_changes, "error_osm_api", changeset, msg)
            except Exception as unknown:
                msg = f"Unknown error for subdivision {sub}: {unknown}"
                logger.exception(msg)
                errors.append(("unknown", msg))
                self._record(sub, sub_changes, "error_unknown", changeset, msg)
            finally:
                # Ensure osmapi's internal changeset state is reset even if an
                # exception occurred mid-upload (osmapi's Changeset context manager
                # does not do this, causing all subsequent departments to fail with
                # "Changeset already opened").
                if self.api._current_changeset_id:  # noqa: SLF001 — see OsmApi  # pyright: ignore[reportPrivateUsage]
                    try:
                        self.api.changeset_close()
                    except Exception:  # noqa: BLE001 — the close failing is the very case
                        self.api._current_changeset_id = 0  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]

        return errors

    def _record(
        self,
        sub: str,
        sub_changes: list[Change],
        status: str,
        changeset: int | None,
        comment: str | None,
    ) -> None:
        """One entry per subdivision — becomes a row of import_subdivisions.
        `tag_counts` is frozen here rather than recomputed later: after the next
        refresh the matches are gone, and with them the only way to tell which
        tag was written, and how many times.
        """
        tag_counts: dict[str, int] = {}
        for change in sub_changes:
            for tag in changed_tags(change):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        self.results.append(
            {
                "subdivision_code": sub,
                "subdivision_name": sub_changes[0]["subdivision_name"],
                "items_count": len(sub_changes),
                "tag_counts": tag_counts,
                "osm_changeset_id": changeset,
                "status": status,
                "comment": comment,
            }
        )

    def _write_osc(
        self,
        changeset: int,
        element_type: str,
        element_id: int,
        action: str,
        data: Element,
    ) -> None:
        osc_dir = Path("./data/atp2osm/changesets")
        osc_dir.mkdir(parents=True, exist_ok=True)
        osc_path = osc_dir / f"{changeset}_{element_type}_{element_id}.osc"

        root = ET.Element("osmChange", version="0.6")
        action_el = ET.SubElement(root, action)
        el = ET.SubElement(
            action_el,
            element_type,
            {
                "id": str(element_id),
                "version": str(data.get("version", "")),
                "changeset": str(changeset),
            },
        )

        if element_type == "node":
            if "lat" in data:
                el.set("lat", str(data["lat"]))
            if "lon" in data:
                el.set("lon", str(data["lon"]))
        elif element_type == "way":
            refs: list[int] = data.get("nd") or []
            for ref in refs:
                ET.SubElement(el, "nd", ref=str(ref))
        elif element_type == "relation":
            members: list[dict[str, str]] = data.get("member") or []
            for member in members:
                ET.SubElement(
                    el,
                    "member",
                    {
                        "type": member["type"],
                        "ref": str(member["ref"]),
                        "role": member.get("role", ""),
                    },
                )

        tags: dict[str, str] = data.get("tag") or {}
        for k, v in tags.items():
            ET.SubElement(el, "tag", k=k, v=str(v))

        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        with osc_path.open("wb") as f:
            tree.write(f, encoding="utf-8", xml_declaration=True)

        logger.debug("DEV: OSC written to %s", osc_path)

    def _sorted_by_subdivision(self) -> dict[str, list[Change]]:
        sorted_changes: dict[str, list[Change]] = {}
        for change in self.changes:
            sub = change["subdivision_code"]
            if sub in sorted_changes:
                sorted_changes[sub].append(change)
            else:
                sorted_changes[sub] = [change]

        return sorted_changes
