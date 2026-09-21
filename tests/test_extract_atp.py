"""The ATP zip: what is extracted from it, and where it lands."""

import zipfile
from pathlib import Path

import pytest

from src.pipeline import atp


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(atp, "ATP_DIR", tmp_path)
    monkeypatch.setattr(atp, "GEOJSON_DIR", tmp_path / "geojson")
    return tmp_path


def zipped(path: Path, names: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            zf.writestr(name, '{"type":"FeatureCollection","features":[]}')


def test_the_spiders_of_the_country_are_extracted_flat(workdir: Path) -> None:
    """ATP nests them under output/; the next steps read one directory."""
    zipped(
        workdir / "output.zip",
        [
            "output/babylone_fr.geojson",
            "output/babylone_mq.geojson",
            "output/babylone.geojson",  # no country suffix: kept
            "output/babylone_de.geojson",  # a foreign country: dropped
            "output/nz_addresses.geojson",  # an address dataset: dropped
            "output/notes.txt",
        ],
    )

    atp.extract_atp()

    assert sorted(p.name for p in (workdir / "geojson").glob("*.geojson")) == [
        "babylone.geojson",
        "babylone_fr.geojson",
        "babylone_mq.geojson",
    ]


def test_a_previous_extraction_is_replaced(workdir: Path) -> None:
    (workdir / "geojson").mkdir()
    (workdir / "geojson" / "stale.geojson").write_text("x")
    zipped(workdir / "output.zip", ["output/babylone_fr.geojson"])
    atp.extract_atp()
    assert [p.name for p in (workdir / "geojson").glob("*.geojson")] == ["babylone_fr.geojson"]


def test_a_zip_without_a_spider_of_ours_is_an_error(workdir: Path) -> None:
    zipped(workdir / "output.zip", ["output/babylone_de.geojson"])
    with pytest.raises(FileNotFoundError):
        atp.extract_atp()


def test_no_zip_means_nothing_was_downloaded(workdir: Path) -> None:
    atp.extract_atp()
    assert not (workdir / "geojson").exists()
