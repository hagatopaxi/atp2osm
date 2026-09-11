import datetime
import json
import logging
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import osmapi
from flask_babel import gettext

from src.config import get_settings
from src.matching import BATCH_MAX_SIZE, changed_tags, subdivision_names
from osmapi.errors import ApiError
from requests_oauthlib import OAuth2Session

logger = logging.getLogger(__name__)


class _FakeOsmApi:
    """Stands in for the OSM API in development.

    The dev instance does not mirror production data, so node, way and
    relation lookups answer 404 and no upload can succeed against it. This
    records the calls instead of sending them, and writes the OSC of each one
    so the generated content can be read. It is also what the tests drive:
    the single code path of `upload` is then the one production runs.
    """

    def __init__(self, osc_writer=None):
        self.calls = []
        self.osc_writer = osc_writer
        self._current_changeset_id = 0
        self._next_id = 0

    def changeset_create(self, tags):
        self._next_id += 1
        self._current_changeset_id = self._next_id
        self.calls.append(("changeset_create", tags))
        return self._current_changeset_id

    def changeset_upload(self, changes):
        self.calls.append(("changeset_upload", changes))
        for block in changes:
            for data in block["data"]:
                self._osc(block["type"], data["id"], block["action"], data)

    def way_update(self, data):
        self.calls.append(("way_update", data))
        self._osc("way", data["id"], "modify", data)

    def relation_update(self, data):
        self.calls.append(("relation_update", data))
        self._osc("relation", data["id"], "modify", data)

    def changeset_close(self):
        self.calls.append(("changeset_close", self._current_changeset_id))
        self._current_changeset_id = 0

    def _osc(self, element_type, element_id, action, data):
        if self.osc_writer:
            self.osc_writer(
                self._current_changeset_id, element_type, element_id, action, data
            )


class BulkUpload:
    """
    Bulk uploads a changeset to the OSM server.
    Batch by subdivision and brand wikidata
    """

    def __init__(
        self,
        changes: list,
        session: OAuth2Session,
        max_size: int = BATCH_MAX_SIZE,
    ):
        # Last gate before an irreversible send, and the only one at the point
        # where changesets are actually created. select_batch already truncates
        # to that size and the route checks it again; if both were ever wrong,
        # nothing must leave for OSM. The size is the wave's: wave 2 sends one
        # POI at a time, and must not be allowed a hundred.
        if len(changes) > max_size:
            raise ValueError(
                f"refusing to upload {len(changes)} POIs, over the {max_size} limit"
            )

        self.changes = changes
        # An empty batch is a no-op rather than a crash: `upload` and
        # `save_log_file` already return early on one.
        self.brand_name = changes[0]["atp_brand"] if changes else ""
        self.brand_wikidata = (
            changes[0]["tag"].get("brand:wikidata") if changes else None
        ) or "unknown"
        self.changesets = []
        self.uploaded_changes = []  # POIs whose subdivision changeset succeeded
        self.results = []  # one entry per subdivision, mirrors import_subdivisions

        settings = get_settings()
        self.api = (
            _FakeOsmApi(osc_writer=self._write_osc)
            if settings.is_dev
            else osmapi.OsmApi(api=settings.api_url, session=session)
        )

    def save_log_file(self) -> Path | None:
        if len(self.changes) == 0:
            logger.info("No change in this run, no log saved.")
            return None

        save_path = Path(
            f"./logs/{self.brand_wikidata}/{datetime.datetime.now().strftime('%Y-%m-%d')}.json"
        )

        # If the save directory doesn't exist, create it
        os.makedirs(save_path.parent, exist_ok=True)

        with open(save_path, "w") as file:
            json.dump(
                {"changes": self.changes, "changesets": self.changesets},
                file,
                indent=4,
                ensure_ascii=False,
            )

        logger.debug(f"Logs for the run saved into {save_path}")
        return save_path

    def upload(self) -> list[tuple[str, str]]:
        """Upload all changes. Returns a list of (error_type, message) tuples; empty list means full success.
        error_type is 'osm_api' for OSM API errors (ApiError), 'unknown' for unexpected exceptions.

        The changeset comment follows the language the contributor is browsing
        in. That is a language of the country — LOCALES holds the country's
        languages — so the community that reviews the changeset can read it,
        whoever uploaded it."""
        if len(self.changes) == 0:
            return []

        changes_by_subdivision = self._sorted_by_subdivision()
        names = subdivision_names(self.changes)
        errors = []

        for sub, sub_changes in changes_by_subdivision.items():
            changeset = None
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
                logger.debug(
                    f"{get_settings().api_url}/changeset/{changeset}"
                )

                changingNodes = []
                for poi in sub_changes:
                    poi["changeset"] = changeset

                    if poi["node_type"] == "node":
                        changingNodes.append(poi)
                    elif poi["node_type"] == "way":
                        self.api.way_update({
                            "id": poi["id"],
                            "version": poi["version"],
                            "changeset": changeset,
                            "tag": poi["tag"],
                            "nd": poi["members"],
                        })
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

                if changingNodes:
                    self.api.changeset_upload(
                        [{"type": "node", "action": "modify", "data": changingNodes}]
                    )

                self.api.changeset_close()
                self.changesets.append(changeset)
                self.uploaded_changes.extend(sub_changes)
                self._record(sub, sub_changes, "success", changeset, None)
            except ApiError as error:
                payload = error.payload.decode("utf-8", errors="replace") if isinstance(error.payload, bytes) else str(error.payload)
                msg = f"OSM API error for subdivision {sub}: HTTP {error.status} — {payload}"
                logger.error(msg)
                errors.append(("osm_api", msg))
                # changeset is None only when its creation failed: it does
                # exist when it is the upload that gave up.
                self._record(sub, sub_changes, "error_osm_api", changeset, msg)
            except Exception as unknown:
                msg = f"Unknown error for subdivision {sub}: {unknown}"
                logger.error(msg)
                errors.append(("unknown", msg))
                self._record(sub, sub_changes, "error_unknown", changeset, msg)
            finally:
                # Ensure osmapi's internal changeset state is reset even if an
                # exception occurred mid-upload (osmapi's Changeset context manager
                # does not do this, causing all subsequent departments to fail with
                # "Changeset already opened").
                if self.api._current_changeset_id:
                    try:
                        self.api.changeset_close()
                    except Exception:
                        self.api._current_changeset_id = 0

        return errors

    def _record(self, sub, sub_changes, status, changeset, comment):
        """One entry per subdivision — becomes a row of import_subdivisions.

        `tag_counts` is frozen here rather than recomputed later: after the next
        refresh the matches are gone, and with them the only way to tell which
        tag was written, and how many times."""
        tag_counts = {}
        for change in sub_changes:
            for tag in changed_tags(change):
                tag_counts[tag] = tag_counts.get(tag, 0) + 1
        self.results.append({
            "subdivision_code": sub,
            "subdivision_name": sub_changes[0]["subdivision_name"],
            "items_count": len(sub_changes),
            "tag_counts": tag_counts,
            "osm_changeset_id": changeset,
            "status": status,
            "comment": comment,
        })

    def _write_osc(
        self,
        changeset: int,
        element_type: str,
        element_id: int,
        action: str,
        data: dict,
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
            for ref in data.get("nd") or []:
                ET.SubElement(el, "nd", ref=str(ref))
        elif element_type == "relation":
            for member in data.get("member") or []:
                ET.SubElement(
                    el,
                    "member",
                    {
                        "type": member["type"],
                        "ref": str(member["ref"]),
                        "role": member.get("role", ""),
                    },
                )

        for k, v in (data.get("tag") or {}).items():
            ET.SubElement(el, "tag", k=k, v=str(v))

        tree = ET.ElementTree(root)
        ET.indent(tree, space="  ")
        with open(osc_path, "wb") as f:
            tree.write(f, encoding="utf-8", xml_declaration=True)

        logger.debug(f"DEV: OSC written to {osc_path}")

    def _sorted_by_subdivision(self):
        sorted_changes = {}
        for change in self.changes:
            sub = change["subdivision_code"]
            if sub in sorted_changes:
                sorted_changes[sub].append(change)
            else:
                sorted_changes[sub] = [change]

        return sorted_changes
