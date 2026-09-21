import logging
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import psycopg

from src.config import ConfigError, get_database, get_settings
from src.phone import ensure_normalize_phone
from src.pipeline.dag import PIPELINE, record_failure
from src.pipeline.errors import PipelineIncompleteError
from src.pipeline.osm import forget_geofabrik_timestamp
from src.pipeline.runner import StepFormatter, main

handler = logging.StreamHandler()
handler.setFormatter(
    StepFormatter(
        fmt="[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)
logger = logging.getLogger(__name__)

# The refresh container's entry point: one crontab line from the
# configuration, then supercronic — which waits for a running job when asked
# to stop, so a deploy never cuts a refresh short. Before any check the job
# itself does: the scheduler must come up even while the sources are down.
# Whatever goes wrong here is a message and exit 1, never a traceback: the
# container restarts on failure and its log is what the deployer reads.
if sys.argv[1:] == ["setup"]:
    try:
        settings = get_settings()
    except ConfigError:
        logger.exception("Configuration refused")
        sys.exit(1)
    supercronic = shutil.which("supercronic")
    if supercronic is None:
        logger.error("supercronic is not installed — this command runs in the container image")
        sys.exit(1)
    # The container's own /tmp: nothing else writes there.
    crontab = Path("/tmp/crontab")  # noqa: S108
    crontab.write_text(f"{settings.refresh_schedule} uv run --no-sync python -m src.pipeline\n")
    # Validate first: a bad schedule is a configuration error to name, not a
    # crash loop to decipher.
    checked = subprocess.run([supercronic, "-test", str(crontab)], capture_output=True, check=False)  # noqa: S603 — the path shutil.which found
    if checked.returncode != 0:
        logger.error(
            "app.refresh_schedule is not a cron expression: '%s'", settings.refresh_schedule
        )
        sys.exit(1)
    os.environ["TZ"] = settings.country.timezone
    logger.info(
        "Refresh scheduled at '%s' %s", settings.refresh_schedule, settings.country.timezone
    )
    # execv replaces this process: nothing below runs on success.
    try:
        os.execv(supercronic, [supercronic, "-passthrough-logs", str(crontab)])  # noqa: S606 — same path
    except OSError:
        logger.exception("Cannot start supercronic")
        sys.exit(1)

get_database()  # fail fast if the DB env vars are missing

# The phone key belongs to the country, so it is installed rather than
# migrated. Before any step rebuilds an index that is built on it.
with psycopg.connect(get_database().conninfo) as _conn:
    ensure_normalize_phone(_conn)

# No internet (the nightly run has hit DNS outages): stop before any step opens
# a data_imports row, so nothing is left half-done. Tomorrow's run retries.
try:
    socket.getaddrinfo("download.geofabrik.de", 443)
except OSError:
    logger.exception("No internet access — aborting, tomorrow's run will retry")
    sys.exit(1)

# A run that crashed left its timestamp file behind; it says nothing about
# today's Geofabrik. The cleanup step removes it on a clean run, this covers
# the rest.
forget_geofabrik_timestamp()

try:
    main(PIPELINE, record_failure)
except PipelineIncompleteError:
    # Everything else ran; the next run picks up what this one could not.
    logger.exception("Datasource unavailable — the next run will retry")
    sys.exit(1)
