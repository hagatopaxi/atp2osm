import logging
import socket
import sys

import psycopg

from src.config import get_database
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

get_database()  # fail fast if the DB env vars are missing

# The phone key belongs to the country, so it is installed rather than
# migrated. Before any step rebuilds an index that is built on it.
with psycopg.connect(**get_database().connect_kwargs) as _conn:
    ensure_normalize_phone(_conn)

# No internet (the nightly run has hit DNS outages): stop before any step opens
# a data_imports row, so nothing is left half-done. The timer retries tomorrow.
try:
    socket.getaddrinfo("download.geofabrik.de", 443)
except OSError as exc:
    logging.error("No internet access (%s) — aborting, the timer will retry tomorrow", exc)
    sys.exit(1)

# A run that crashed left its timestamp file behind; it says nothing about
# today's Geofabrik. The cleanup step removes it on a clean run, this covers
# the rest.
forget_geofabrik_timestamp()

try:
    main(PIPELINE, record_failure)
except PipelineIncomplete as exc:
    # Everything else ran; exiting non-zero is what makes systemd retry in 4h.
    logging.error("Datasource unavailable (%s) — retrying in 4h", exc)
    sys.exit(1)
