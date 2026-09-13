#!/bin/bash
set -euo pipefail

# Runs the full pipeline inside the app container.
# The container (atp2osm:latest) already has Python + osm2pgsql installed.
# Systemd timer calls this script.

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_NAME=$(basename "$PROJECT_DIR")
IMAGE_NAME="${PROJECT_NAME//./-}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "Starting data refresh pipeline..."
podman run --rm \
    --network host \
    --env-file "$PROJECT_DIR/.env" \
    --env ATP2OSM_CONFIG=/app/config.json \
    --env ATP2OSM_FORCE \
    -v "$PROJECT_DIR/config.json:/app/config.json:ro,Z" \
    -v "$PROJECT_DIR/data:/app/data:Z" \
    "${IMAGE_NAME}:latest" \
    uv run --no-sync python -m src.pipeline "$@"
log "Data refresh complete"
