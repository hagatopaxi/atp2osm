"""
Shared constants for the ATP2OSM import pipeline.
"""

from pathlib import Path

from src.config import get_country, get_pipeline

# Parallel processing configuration
WORKERS = get_pipeline().workers

# File size limits
MAX_FILE_SIZE = 128 * 1024 * 1024  # 128 MB - maximum size for NDJSON chunks

# Directory paths
PROJECT_ROOT = Path(__file__).parent.parent.parent
ATP_DIR = PROJECT_ROOT / "data" / "atp"
GEOJSON_DIR = ATP_DIR / "geojson"
NDGEOJSON_DIR = ATP_DIR / "ndgeojson"
SPLIT_DIR = ATP_DIR / "split"
PARQUET_PATH = ATP_DIR / "latest.parquet"
SPIDERS_PATH = ATP_DIR / "spiders.json"
ATP_HISTORY_URL = "https://data.alltheplaces.xyz/runs/history.json"
GEOFABRIK_BASE = "https://download.geofabrik.de"

# NSI (name-suggestion-index). dist/ is no longer committed on the GitHub main
# branch, so npm + jsDelivr is the only channel.
#
# The version is always pinned, and not only to keep the file in step with the
# data_imports row: jsDelivr answers `@latest` from a cache that can be years
# stale. It still serves a 6.x-era file under /dist/nsi.json, a path this
# package stopped shipping — hence the /dist/json/ one below. A pinned URL is
# resolved against the real tarball, so a wrong path fails loudly with a 404
# instead of silently returning obsolete data.
NSI_DIR = PROJECT_ROOT / "data" / "nsi"

# Where a run parks the Geofabrik timestamp it has already fetched, so the
# three steps that need it query the network once. Written at the probe,
# removed by the cleanup step, and cleared again at the start of every run —
# a file outlives a crashed process, unlike the value it stands for.
GEOFABRIK_TS_PATH = PROJECT_ROOT / "data" / "osm" / "geofabrik-timestamp.txt"
NSI_PATH = NSI_DIR / "nsi.json"
NSI_REGISTRY_URL = "https://registry.npmjs.org/name-suggestion-index"
NSI_CDN_URL = (
    "https://cdn.jsdelivr.net/npm/name-suggestion-index@{version}/dist/json/nsi.json"
)

# The finest administrative level a POI is attached to, and the deepest one
# imported: the attachment falls back down to 2 (the country) when no polygon of
# that level covers it, so anything below is dead weight.
ADMIN_LEVEL = get_country().admin_level
ADMIN_LEVEL_MAX = get_country().admin_level_max

# One entry per Geofabrik extract of the country. The region name is the last
# path segment, which is also what names the PBF file.
GEOFABRIK_REGIONS = {
    name: {
        "url":      f"{GEOFABRIK_BASE}/{path}-latest.osm.pbf",
        "state_url": f"{GEOFABRIK_BASE}/{path}-updates/state.txt",
        "pbf_path": PROJECT_ROOT / "data" / "osm" / f"{path.split('/')[-1]}-latest.osm.pbf",
    }
    for name, path in get_country().geofabrik_regions.items()
}
