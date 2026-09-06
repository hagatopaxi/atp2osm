"""Settings, read from one JSON file plus the secrets in the environment.

`config.schema.json` describes that file — shape, types and what each setting
is for — and validates it at startup. It is the documentation: a setting is
explained where it is declared, so the explanation cannot drift from the
loader. What is left here is what a schema cannot express: a language Babel
knows, a real IANA timezone, one level below another.

Secrets stay in the environment: a password does not belong in a file meant to
be read, mounted and diffed.
"""

import json
import logging
import os
import pathlib
import zoneinfo
from dataclasses import dataclass, field
from functools import lru_cache

import jsonschema
from babel import Locale, UnknownLocaleError

logger = logging.getLogger(__name__)

PROJECT_ROOT = pathlib.Path(__file__).parent.parent.resolve()
TEMPLATE_DIR = PROJECT_ROOT / "website" / "templates"
TRANSLATIONS_DIR = PROJECT_ROOT / "website" / "translations"
CACHE_DIR = PROJECT_ROOT / ".cache"
STATIC_DIR = PROJECT_ROOT / "static"

SCHEMA_PATH = PROJECT_ROOT / "config.schema.json"


class ConfigError(RuntimeError):
    """Raised when the configuration file or a required secret is missing or invalid."""


def get_env(name: str) -> str:
    """Get a required environment variable or raise ConfigError."""
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text())


def _validate(document) -> None:
    """Raise ConfigError naming the setting at fault, not a jsonschema dump."""
    try:
        jsonschema.validate(document, _schema())
    except jsonschema.ValidationError as error:
        where = ".".join(str(part) for part in error.absolute_path) or "configuration"
        raise ConfigError(f"{where}: {error.message}") from None


def _default(*path):
    """The default the schema documents, so it is written down once."""
    node = _schema()
    for key in path:
        node = node["properties"][key]
    return node["default"]


@dataclass(frozen=True)
class Country:
    """Everything that differs from one country to the next."""

    territory_codes: tuple[str, ...]
    locales: tuple[str, ...]
    timezone: str
    geofabrik: tuple[str, ...]
    admin_level: int
    admin_level_max: int
    match_radius_m: int
    nsi_locations: frozenset[str]
    nsi_writable_tags: frozenset[str]
    nsi_calibration: dict = field(default_factory=dict, compare=False)

    @property
    def code(self) -> str:
        """The country itself, which its territories follow — the first code."""
        return self.territory_codes[0]

    @property
    def geofabrik_regions(self) -> dict[str, str]:
        """Region name → Geofabrik path. The name is the last path segment,
        which is also what names the PBF file."""
        return {path.rsplit("/", 1)[-1]: path for path in self.geofabrik}


@dataclass(frozen=True)
class Database:
    """PostGIS connection settings — shared by app and pipeline."""

    name: str
    user: str
    password: str
    host: str
    port: str

    @property
    def connect_kwargs(self) -> dict:
        return {
            "dbname": self.name,
            "user": self.user,
            "password": self.password,
            "host": self.host,
            "port": self.port,
        }


@dataclass(frozen=True)
class Pipeline:
    """Pipeline settings — how hard the refresh is allowed to push the host."""

    workers: int
    min_free_gb: float


@dataclass(frozen=True)
class Settings:
    """Everything the application reads: the deployment, its country, its database."""

    env: str
    api_url: str
    oauth_client_id: str
    oauth_client_secret: str
    app_base_url: str
    source_repo_url: str
    translations_dir: str
    refresh_schedule: str
    secret_key: str
    port: int
    app_version: str
    country: Country
    db: Database
    pipeline: Pipeline

    @property
    def is_dev(self) -> bool:
        return self.env == "DEVELOPMENT"


def get_version() -> str:
    """Application version. GIT_COMMIT is set by the container build."""
    rev = os.environ.get("GIT_COMMIT")
    if not rev:
        if os.environ.get("APP_ENV", "").upper() == "PRODUCTION":
            raise ConfigError("GIT_COMMIT must be set in production")
        return "Gamma"
    return f"Gamma-{rev}"


def _parse_country(raw: dict) -> Country:
    """Build the country. The schema has vouched for shapes and types already."""
    locales = tuple(raw["locales"])
    for locale in locales:
        try:
            Locale.parse(locale)
        except (UnknownLocaleError, ValueError):
            raise ConfigError(f"country.locales holds an unknown language: '{locale}'")

    timezone = raw.get("timezone", _default("country", "timezone"))
    try:
        zoneinfo.ZoneInfo(timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"country.timezone is not an IANA timezone: '{timezone}'")

    admin_level = raw["admin_level"]
    admin_level_max = raw.get("admin_level_max", _default("country", "admin_level_max"))
    if admin_level_max < admin_level:
        raise ConfigError(
            f"country.admin_level_max ({admin_level_max}) is below "
            f"country.admin_level ({admin_level})"
        )

    writable = tuple(raw.get("nsi_writable_tags", _default("country", "nsi_writable_tags")))
    calibration = raw.get("nsi_calibration", {})
    unmeasured = set(writable) - set(calibration.get("agreement") or {})
    if unmeasured:
        # A tag written without a measurement behind it produces no error, only
        # badly tagged objects — see scripts/calibrate_nsi_tags.py.
        logger.warning(
            "NSI writes %s without a measured agreement rate", sorted(unmeasured)
        )

    return Country(
        territory_codes=tuple(raw["territory_codes"]),
        locales=locales,
        timezone=timezone,
        geofabrik=tuple(raw["geofabrik"]),
        admin_level=admin_level,
        admin_level_max=admin_level_max,
        match_radius_m=raw.get("match_radius_m", _default("country", "match_radius_m")),
        nsi_locations=frozenset(raw["nsi_locations"]),
        nsi_writable_tags=frozenset(writable),
        nsi_calibration=calibration,
    )


def _parse_app(raw: dict, country: Country) -> Settings:
    """Build the deployment settings, taking the secrets from the environment."""
    db = raw["db"]
    pipeline = raw.get("pipeline", {})
    return Settings(
        env=raw.get("env", _default("app", "env")).upper(),
        api_url=raw["osm_api_host"].strip("/"),
        app_base_url=raw["base_url"].rstrip("/"),
        # An empty value falls back too: a deployment that has no repository of
        # its own leaves the field rather than deleting the key.
        source_repo_url=raw.get("source_repo_url")
        or _default("app", "source_repo_url"),
        translations_dir=raw.get("translations_dir", _default("app", "translations_dir")),
        refresh_schedule=raw.get("refresh_schedule", _default("app", "refresh_schedule")),
        port=raw.get("port", _default("app", "port")),
        db=Database(
            name=db["name"],
            user=db["user"],
            host=db["host"],
            port=str(db.get("port", 5432)),
            password=get_env("OSM_DB_PASSWORD"),
        ),
        pipeline=Pipeline(
            workers=pipeline.get("workers", max(1, (os.cpu_count() or 4) // 2)),
            min_free_gb=float(
                pipeline.get("min_free_gb", _default("app", "pipeline", "min_free_gb"))
            ),
        ),
        oauth_client_id=get_env("OSM_OAUTH_CLIENT_ID"),
        oauth_client_secret=get_env("OSM_OAUTH_CLIENT_SECRET"),
        secret_key=get_env("SECRET_KEY"),
        app_version=get_version(),
        country=country,
    )


def load(raw) -> Settings:
    """Validate a configuration document and build the settings from it."""
    _validate(raw)
    return _parse_app(raw["app"], _parse_country(raw["country"]))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Read ATP2OSM_CONFIG. Fails at startup rather than at first use."""
    path = pathlib.Path(get_env("ATP2OSM_CONFIG"))
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"ATP2OSM_CONFIG points at no file: {path}")
    except json.JSONDecodeError as error:
        raise ConfigError(f"{path} is not valid JSON: {error}")
    return load(raw)


def get_country() -> Country:
    return get_settings().country


def get_database() -> Database:
    return get_settings().db


def get_pipeline() -> Pipeline:
    return get_settings().pipeline
