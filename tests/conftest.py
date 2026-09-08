"""A configuration for the tests, written before anything imports the settings.

No country file ships with the product, so the tests cannot borrow one — they
give themselves a plausible country instead. It is written at import time
rather than in a fixture because `src.pipeline.constants` reads the settings
while it is being imported, which happens at collection.
"""

import json
import os
import tempfile

CONFIG = {
    "country": {
        "territory_codes": ["fr", "mq"],
        "locales": ["fr"],
        "timezone": "Europe/Paris",
        "geofabrik": ["europe/france"],
        "admin_level": 6,
        "admin_level_max": 8,
        "match_radius_m": 500,
        "nsi_locations": ["fr", "150", "eu", "001"],
        "nsi_writable_tags": ["brand:wikidata"],
    },
    "app": {
        "env": "DEVELOPMENT",
        "base_url": "http://localhost:5000",
        "osm_api_host": "https://api.openstreetmap.org",
        "db": {"name": "o2p", "user": "o2p", "host": "127.0.0.1", "port": 5434},
    },
}

_SECRETS = {
    "OSM_DB_PASSWORD": "test",
    "OSM_OAUTH_CLIENT_ID": "test",
    "OSM_OAUTH_CLIENT_SECRET": "test",
    "SECRET_KEY": "test",
}

_path = tempfile.NamedTemporaryFile(
    mode="w", suffix=".json", prefix="atp2osm-test-", delete=False
)
json.dump(CONFIG, _path)
_path.close()

# The real database settings win when they are given: the tests that need a
# live PostGIS read them from the environment, as they always did.
os.environ.setdefault("ATP2OSM_CONFIG", _path.name)
for _name, _value in _SECRETS.items():
    os.environ.setdefault(_name, _value)
