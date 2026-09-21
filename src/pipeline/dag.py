# Pipeline DAG — each entry is a step function (None for the start node), the
# names of its successors, and optional options.
#
# Options:
#   lock: "<name>" — steps sharing the same lock name are serialized via a
#                    mutex; only one runs at a time, others queue behind it.
#                    Use for bandwidth-heavy operations (e.g. lock="network")
#                    where true concurrency would be counterproductive.
#
# Execution model: each branch runs independently — a step starts as soon as
# all its direct predecessors are done, with no synchronisation barrier between
# unrelated branches.
#
# To add a step: implement a function in osm.py / atp.py / atp2osm.py,
# import it here, and wire it into PIPELINE.

import logging
import traceback

from src.pipeline._db import connect, last_import_date, record_import
from src.pipeline.atp import (
    cleanup_atp,
    create_parquet_atp,
    download_atp,
    extract_atp,
    import_atp,
)
from src.pipeline.atp2osm import create_mv_places_brand
from src.pipeline.errors import SourceUnavailableError
from src.pipeline.ndgeojson_to_parquet import convert_atp, split_atp
from src.pipeline.nsi import download_nsi, import_nsi
from src.pipeline.osm import (
    download_pbf,
    probe_osm_freshness,
    run_osm2pgsql,
    setup_mv_places,
)
from src.pipeline.runner import Pipeline

logger = logging.getLogger(__name__)

PIPELINE: Pipeline = {
    "start": (None, ["osm-probe", "atp-download", "nsi-download"]),
    # Unlocked on purpose: the freshness probe can retry for minutes when
    # Geofabrik is slow, and it must not hold "network" while it sleeps.
    "osm-probe": (probe_osm_freshness, ["osm-download"]),
    "osm-download": (download_pbf, ["osm-import"], {"lock": "network"}),
    # atp-import too: it attaches each POI to a subdivision, and subdivisions
    # is one of the tables osm2pgsql writes.
    "osm-import": (run_osm2pgsql, ["osm-views", "atp-import"], {"lock": "cpu"}),
    "osm-views": (setup_mv_places, ["mv-brand"]),
    "nsi-download": (download_nsi, ["nsi-import"], {"lock": "network"}),
    # Before osm-views: setup_mv_places completes brand:wikidata from nsi_brands.
    "nsi-import": (import_nsi, ["osm-views"]),
    "atp-download": (download_atp, ["atp-extract"], {"lock": "network"}),
    "atp-extract": (extract_atp, ["atp-convert"], {"lock": "cpu"}),
    "atp-convert": (convert_atp, ["atp-split"], {"lock": "cpu"}),
    "atp-split": (split_atp, ["atp-parquet"], {"lock": "cpu"}),
    "atp-parquet": (create_parquet_atp, ["atp-import"], {"lock": "cpu"}),
    "atp-import": (import_atp, ["mv-brand"]),
    "mv-brand": (create_mv_places_brand, ["cleanup"]),
    "cleanup": (cleanup_atp, []),
}


def record_failure(step_name: str, exc: BaseException) -> None:
    """Failure hook for the runner: close the branch's open row on the failing
    step, keeping its full stack trace so a refresh can be diagnosed later.

    Left 'pending', not 'error': the tables are the ones the last resolved row
    describes — a rebuild swaps its object in at the end, so a failed one
    changed nothing — and the guards read that row's comment, which an
    'error' row carrying a stack trace would shadow. The next run supersedes
    it.

    A source that was merely unreachable is resolved 'skipped' instead,
    keeping its previous date so the displayed source date does not go
    backwards. The 4-hourly retry overwrites it with a real import if the
    source comes back.

    Opens its own connection (the step's own one may be in a broken
    transaction) and never raises — masking the original error would be worse.
    """
    comment = f"step '{step_name}' failed\n" + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__)
    )
    # mv-brand, cleanup, … don't belong to the osm/atp/nsi branches.
    import_type = step_name.split("-", 1)[0]
    if import_type not in ("osm", "atp", "nsi"):
        import_type = "pipeline"
    try:
        conn = connect()
        try:
            if isinstance(exc, SourceUnavailableError):
                record_import(
                    conn, import_type, last_import_date(conn, import_type), "skipped", comment
                )
            else:
                record_import(conn, import_type, None, "pending", comment)
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not record failure for step '%s'", step_name)
