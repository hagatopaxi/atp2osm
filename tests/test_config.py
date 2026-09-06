"""The loader is a trust boundary: the file it reads lives outside the repo."""

import copy
import json

import pytest

from src import config
from tests.conftest import CONFIG


def load(**changes):
    """Load the reference document, with the given sections merged over it."""
    document = copy.deepcopy(CONFIG)
    for section, values in changes.items():
        document[section] = {**document[section], **values}
    return config.load(document)


def drop(section, key):
    document = copy.deepcopy(CONFIG)
    del document[section][key]
    return document


def test_a_complete_document_arrives_intact():
    settings = load()

    assert settings.country.code == "fr"
    assert settings.country.locales == ("fr",)
    assert settings.country.geofabrik == ("europe/france",)
    assert settings.country.geofabrik_regions == {"france": "europe/france"}
    assert settings.country.admin_level == 6
    assert settings.country.match_radius_m == 500
    assert settings.api_url == "https://api.openstreetmap.org"
    assert settings.db.name == "o2p"
    assert settings.db.port == "5434"
    assert settings.is_dev


def test_the_optional_settings_have_defaults():
    document = copy.deepcopy(CONFIG)
    for key in ("admin_level_max", "match_radius_m", "nsi_writable_tags", "timezone"):
        document["country"].pop(key, None)
    settings = config.load(document)

    assert settings.country.admin_level_max == 8
    assert settings.country.match_radius_m == 500
    assert settings.country.nsi_writable_tags == frozenset({"brand:wikidata"})
    assert settings.country.timezone == "UTC"
    assert settings.source_repo_url.endswith("/atp2osm-import")
    assert load(app={"source_repo_url": ""}).source_repo_url == settings.source_repo_url
    assert settings.refresh_schedule == "*-*-* 04:00:00"
    assert settings.pipeline.min_free_gb == 15.0


@pytest.mark.parametrize(
    "section, key",
    [
        ("country", "code"),
        ("country", "locales"),
        ("country", "geofabrik"),
        ("country", "admin_level"),
        ("country", "nsi_locations"),
        ("app", "base_url"),
        ("app", "osm_api_host"),
        ("app", "db"),
    ],
)
def test_a_required_setting_is_named_when_it_is_missing(section, key):
    with pytest.raises(config.ConfigError, match=key):
        config.load(drop(section, key))


@pytest.mark.parametrize(
    "section, key, value",
    [
        ("country", "code", 33),
        ("country", "admin_level", "6"),
        ("country", "admin_level", True),  # a bool is an int, and never a level
        ("country", "locales", "fr"),  # a string is not a list of strings
        ("country", "geofabrik", []),
        ("country", "nsi_locations", [""]),
        ("app", "port", "8000"),
        ("app", "db", "o2p"),
    ],
)
def test_a_setting_of_the_wrong_type_is_refused(section, key, value):
    with pytest.raises(config.ConfigError, match=key):
        load(**{section: {key: value}})


@pytest.mark.parametrize("section", ["country", "app"])
def test_an_unknown_setting_is_refused_rather_than_ignored(section):
    """A typo that is ignored is a setting that silently keeps its default."""
    with pytest.raises(config.ConfigError, match="admin_levl"):
        load(**{section: {"admin_levl": 6}})


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"country": {"code": "FR"}}, "country.code"),
        ({"country": {"code": "fra"}}, "country.code"),
        ({"country": {"locales": ["fr", "zz"]}}, "zz"),
        ({"country": {"timezone": "Mars/Olympus"}}, "Mars/Olympus"),
        ({"country": {"admin_level": 6, "admin_level_max": 4}}, "admin_level_max"),
        ({"app": {"env": "STAGING"}}, "app.env"),
    ],
)
def test_a_value_that_cannot_work_is_refused_at_startup(changes, message):
    with pytest.raises(config.ConfigError, match=message):
        load(**changes)


def test_an_unmeasured_nsi_tag_is_warned_about(caplog):
    """A tag written without a measurement behind it mistags in silence."""
    load(country={"nsi_writable_tags": ["brand:wikidata", "official_name"]})
    assert "official_name" in caplog.text

    caplog.clear()
    load(
        country={
            "nsi_writable_tags": ["brand:wikidata"],
            "nsi_calibration": {"agreement": {"brand:wikidata": 1.0}},
        }
    )
    assert not caplog.text


def test_the_file_itself_is_refused_when_it_cannot_be_read(tmp_path, monkeypatch):
    config.get_settings.cache_clear()
    monkeypatch.setenv("ATP2OSM_CONFIG", str(tmp_path / "nowhere.json"))
    with pytest.raises(config.ConfigError, match="points at no file"):
        config.get_settings()

    broken = tmp_path / "broken.json"
    broken.write_text("{oops")
    monkeypatch.setenv("ATP2OSM_CONFIG", str(broken))
    with pytest.raises(config.ConfigError, match="not valid JSON"):
        config.get_settings()

    monkeypatch.delenv("ATP2OSM_CONFIG")
    with pytest.raises(config.ConfigError, match="ATP2OSM_CONFIG"):
        config.get_settings()

    config.get_settings.cache_clear()


def test_a_secret_stays_in_the_environment(monkeypatch):
    """The file is meant to be read, mounted and diffed; a password is not."""
    monkeypatch.delenv("OSM_DB_PASSWORD")
    with pytest.raises(config.ConfigError, match="OSM_DB_PASSWORD"):
        load()


def test_the_reference_document_is_what_the_file_would_hold(tmp_path, monkeypatch):
    """The dict the tests share and a real file on disk go through one path."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG))
    config.get_settings.cache_clear()
    monkeypatch.setenv("ATP2OSM_CONFIG", str(path))

    assert config.get_settings().country == load().country
    config.get_settings.cache_clear()


def test_the_example_the_schema_carries_is_a_document_the_loader_accepts():
    """The schema documents and exemplifies; nothing else has to stay in step.

    Filling the blanks the example deliberately leaves must be enough to make it
    load: a key the loader does not know raises here, and so does a required key
    the example forgot to document.
    """
    from src.config import SCHEMA_PATH

    document = json.loads(SCHEMA_PATH.read_text())["examples"][0]
    blanks = {"base_url": "https://atp2osm.example.org"}
    assert all(document["app"][key] == "" for key in blanks), "a blank was filled in"
    document["app"] |= blanks
    document["app"]["db"] |= {"name": "o2p", "user": "o2p", "host": "127.0.0.1"}

    settings = config.load(document)
    assert settings.country.code == "fr"
    assert settings.env == "PRODUCTION"
