import logging
import os
import shutil
import socket
import subprocess
import sys

import psycopg

from src.config import ConfigError, get_database, get_settings
from src.phone import ensure_normalize_phone
from src.pipeline.dag import PIPELINE, record_failure
from src.pipeline.errors import PipelineIncomplete
from src.pipeline.osm import forget_geofabrik_timestamp
from src.pipeline.runner import StepFormatter, main

handler = logging.StreamHandler()
handler.setFormatter(StepFormatter(
    fmt="[%(asctime)s] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(handler)

# The refresh container's entry point: one crontab line from the
# configuration, then supercronic — which waits for a running job when asked
# to stop, so a deploy never cuts a refresh short. Before any check the job
# itself does: the scheduler must come up even while the sources are down.
# Whatever goes wrong here is a message and exit 1, never a traceback: the
# container restarts on failure and its log is what the deployer reads.
if sys.argv[1:] == ["setup"]:
    try:
        settings = get_settings()
    except ConfigError as exc:
        logging.error("Configuration refused: %s", exc)
        sys.exit(1)
    supercronic = shutil.which("supercronic")
    if supercronic is None:
        logging.error("supercronic is not installed — this command runs in the container image")
        sys.exit(1)
    crontab = "/tmp/crontab"
    with open(crontab, "w") as f:
        f.write(f"{settings.refresh_schedule} uv run --no-sync python -m src.pipeline\n")
    # Validate first: a bad schedule is a configuration error to name, not a
    # crash loop to decipher.
    if subprocess.run([supercronic, "-test", crontab], capture_output=True).returncode != 0:
        logging.error("app.refresh_schedule is not a cron expression: '%s'", settings.refresh_schedule)
        sys.exit(1)
    os.environ["TZ"] = settings.country.timezone
    logging.info("Refresh scheduled at '%s' %s", settings.refresh_schedule, settings.country.timezone)
    # execv replaces this process: nothing below runs on success.
    try:
        os.execv(supercronic, [supercronic, "-passthrough-logs", crontab])
    except OSError as exc:
        logging.error("Cannot start supercronic: %s", exc)
        sys.exit(1)

get_database()  # fail fast if the DB env vars are missing

# The phone key belongs to the country, so it is installed rather than
# migrated. Before any step rebuilds an index that is built on it.
with psycopg.connect(**get_database().connect_kwargs) as _conn:
    ensure_normalize_phone(_conn)

# No internet (the nightly run has hit DNS outages): stop before any step opens
# a data_imports row, so nothing is left half-done. Tomorrow's run retries.
try:
    socket.getaddrinfo("download.geofabrik.de", 443)
except OSError as exc:
    logging.error("No internet access (%s) — aborting, tomorrow's run will retry", exc)
    sys.exit(1)

# A run that crashed left its timestamp file behind; it says nothing about
# today's Geofabrik. The cleanup step removes it on a clean run, this covers
# the rest.
forget_geofabrik_timestamp()

try:
    main(PIPELINE, record_failure)
except PipelineIncomplete as exc:
    # Everything else ran; the next run picks up what this one could not.
    logging.error("Datasource unavailable (%s) — the next run will retry", exc)
    sys.exit(1)
