"""ATP's GeoJSON to the parquet DuckDB loads: three file steps, each one
resumable after a crash, and none losing a feature on the way.

The layout under test is the one ATP publishes: a FeatureCollection header on
the first line, one feature per line ending with a comma, `]}` on the last.
"""

import json
from pathlib import Path
from typing import Any

import duckdb
import pytest

from src.pipeline import ndgeojson_to_parquet as ndg
from tests.conftest import one

Feature = dict[str, Any]


def feature(feature_id: str, lon: float | None = 2.35, lat: float = 48.85) -> Feature:
    return {
        "type": "Feature",
        "id": feature_id,
        "properties": {"@spider": "babylone_fr", "name": f"Babylone {feature_id}"},
        "geometry": None if lon is None else {"type": "Point", "coordinates": [lon, lat]},
    }


def collection(features: list[Feature]) -> str:
    """A file as ATP writes it."""
    head = (
        '{"type":"FeatureCollection","dataset_attributes":{"@spider":"babylone_fr"},"features":[\n'
    )
    body = ",\n".join(json.dumps(f) for f in features)
    return head + body + ("\n" if features else "") + "]}\n"


def lines(path: Path) -> list[Feature]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def dirs(tmp_path: Path) -> Path:
    for name in ("geojson", "ndgeojson", "split"):
        (tmp_path / name).mkdir()
    return tmp_path


# --- GeoJSON -> NDJSON --------------------------------------------------------------


def test_every_feature_becomes_one_line_and_the_source_is_consumed(dirs: Path) -> None:
    src = dirs / "geojson" / "babylone_fr.geojson"
    src.write_text(collection([feature("a"), feature("b"), feature("c")]))

    ndg.convert_geojson_to_ndgeojson(dirs / "geojson", dirs / "ndgeojson")

    out = dirs / "ndgeojson" / "babylone_fr.geojson"
    assert [f["id"] for f in lines(out)] == ["a", "b", "c"]
    assert not src.exists()
    assert not out.with_suffix(".geojson.tmp").exists()


def test_a_single_feature_has_no_trailing_comma_to_drop(dirs: Path) -> None:
    (dirs / "geojson" / "one.geojson").write_text(collection([feature("only")]))
    ndg.convert_geojson_to_ndgeojson(dirs / "geojson", dirs / "ndgeojson")
    assert [f["id"] for f in lines(dirs / "ndgeojson" / "one.geojson")] == ["only"]


@pytest.mark.parametrize(
    "content",
    ["", collection([]), '{"type":"FeatureCollection","features":[]}\n'],
    ids=["empty-file", "empty-collection", "one-line-collection"],
)
def test_a_spider_without_features_leaves_nothing_behind(dirs: Path, content: str) -> None:
    src = dirs / "geojson" / "none.geojson"
    src.write_text(content)
    (dirs / "geojson" / "some.geojson").write_text(collection([feature("a")]))

    ndg.convert_geojson_to_ndgeojson(dirs / "geojson", dirs / "ndgeojson")

    assert not src.exists()
    assert sorted(p.name for p in (dirs / "ndgeojson").iterdir()) == ["some.geojson"]


def test_a_crashed_run_resumes_on_what_is_left(dirs: Path) -> None:
    """The NDJSON already written is kept, its source is only dropped."""
    done = dirs / "ndgeojson" / "done.geojson"
    done.write_text(json.dumps(feature("from-the-first-run")) + "\n")
    (dirs / "geojson" / "done.geojson").write_text(collection([feature("would-be-redone")]))
    (dirs / "geojson" / "todo.geojson").write_text(collection([feature("b")]))

    ndg.convert_geojson_to_ndgeojson(dirs / "geojson", dirs / "ndgeojson")

    assert [f["id"] for f in lines(done)] == ["from-the-first-run"]
    assert [f["id"] for f in lines(dirs / "ndgeojson" / "todo.geojson")] == ["b"]
    assert list((dirs / "geojson").iterdir()) == []


def test_nothing_to_convert_is_an_error_not_a_silence(dirs: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ndg.convert_geojson_to_ndgeojson(dirs / "geojson", dirs / "ndgeojson")


def test_the_step_skips_when_the_download_was_skipped(
    dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No zip, no geojson directory: the branch no-ops on its guard."""
    monkeypatch.setattr(ndg, "GEOJSON_DIR", dirs / "absent")
    ndg.convert_atp()
    monkeypatch.setattr(ndg, "GEOJSON_DIR", dirs / "geojson")  # exists, empty
    ndg.convert_atp()


# --- Split ------------------------------------------------------------------------------


def ndjson(path: Path, ids: list[str]) -> None:
    path.write_text("".join(json.dumps(feature(i)) + "\n" for i in ids))


def test_a_small_file_is_moved_whole(dirs: Path) -> None:
    ndjson(dirs / "ndgeojson" / "small.geojson", ["a", "b"])
    ndg.split_ndgeojson(dirs / "ndgeojson", dirs / "split")
    assert [f["id"] for f in lines(dirs / "split" / "small.geojson")] == ["a", "b"]
    assert list((dirs / "ndgeojson").iterdir()) == []


def test_a_big_file_is_cut_on_line_boundaries_without_losing_a_feature(
    dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ndg, "MAX_FILE_SIZE", 300)
    ids = [f"feature-{i:03d}" for i in range(20)]
    src = dirs / "ndgeojson" / "big.geojson"
    ndjson(src, ids)

    ndg.split_ndgeojson(dirs / "ndgeojson", dirs / "split")

    chunks = sorted(
        (dirs / "split").glob("big_*.geojson"), key=lambda p: int(p.stem.rsplit("_", 1)[1])
    )
    assert len(chunks) > 1
    assert all(c.stat().st_size <= 300 for c in chunks)
    # Every chunk is whole lines, and together they are the file.
    assert [f["id"] for c in chunks for f in lines(c)] == ids
    assert not src.exists()


def test_a_line_bigger_than_a_chunk_is_emitted_whole(
    dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard cut would corrupt the JSON object."""
    monkeypatch.setattr(ndg, "MAX_FILE_SIZE", 120)
    huge = feature("huge")
    huge["properties"]["description"] = "x" * 500
    src = dirs / "ndgeojson" / "big.geojson"
    src.write_text(
        json.dumps(feature("small"))
        + "\n"
        + json.dumps(huge)
        + "\n"
        + json.dumps(feature("after"))
        + "\n"
    )

    ndg.split_ndgeojson(dirs / "ndgeojson", dirs / "split")

    chunks = sorted(
        (dirs / "split").glob("big_*.geojson"), key=lambda p: int(p.stem.rsplit("_", 1)[1])
    )
    assert [f["id"] for c in chunks for f in lines(c)] == ["small", "huge", "after"]


def test_a_crashed_split_is_redone_identically(dirs: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ndg, "MAX_FILE_SIZE", 300)
    ids = [f"feature-{i:03d}" for i in range(20)]
    ndjson(dirs / "ndgeojson" / "big.geojson", ids)
    # The first run died after one chunk, leaving it half-written.
    (dirs / "split" / "big_1.geojson").write_text('{"broken":')

    ndg.split_ndgeojson(dirs / "ndgeojson", dirs / "split")

    chunks = sorted(
        (dirs / "split").glob("big_*.geojson"), key=lambda p: int(p.stem.rsplit("_", 1)[1])
    )
    assert [f["id"] for c in chunks for f in lines(c)] == ids


def test_nothing_to_split_is_an_error_not_a_silence(dirs: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ndg.split_ndgeojson(dirs / "ndgeojson", dirs / "split")


def test_the_split_step_skips_when_nothing_was_converted(
    dirs: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ndg, "NDGEOJSON_DIR", dirs / "ndgeojson")
    ndg.split_atp()
    assert list((dirs / "split").iterdir()) == []


# --- NDJSON -> parquet -------------------------------------------------------------------


def read_parquet(path: Path) -> list[tuple[Any, ...]]:
    with duckdb.connect() as con:
        con.load_extension("spatial")
        return con.execute(
            f"SELECT id, properties->>'$.name', ST_X(geom), ST_Y(geom) FROM read_parquet('{path}') ORDER BY id"  # noqa: S608 — a path of the test
        ).fetchall()


def test_the_parquet_holds_every_located_feature(dirs: Path) -> None:
    ndjson(dirs / "split" / "part_1.geojson", ["a", "b"])
    (dirs / "split" / "part_2.geojson").write_text(
        json.dumps(feature("c", 3.0, 44.0)) + "\n" + json.dumps(feature("nowhere", None)) + "\n"
    )
    (dirs / "split" / "empty.geojson").write_text("")

    ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")

    assert read_parquet(dirs / "latest.parquet") == [
        ("a", "Babylone a", 2.35, 48.85),
        ("b", "Babylone b", 2.35, 48.85),
        ("c", "Babylone c", 3.0, 44.0),
    ]
    assert not (dirs / ".duckdb_temp").exists()


def test_the_parquet_carries_its_geoparquet_metadata(dirs: Path) -> None:
    """Written by the spatial extension: it is what makes `geom` read back
    as a geometry rather than a blob.
    """
    ndjson(dirs / "split" / "part.geojson", ["a"])
    (dirs / "split" / "far.geojson").write_text(json.dumps(feature("b", -61.0, 14.6)) + "\n")

    ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")

    with duckdb.connect() as con:
        (raw,) = one(
            con.execute(
                f"SELECT value FROM parquet_kv_metadata('{dirs / 'latest.parquet'}') WHERE key = 'geo'"  # noqa: S608
            ).fetchone()
        )
    geo = json.loads(raw)
    assert geo["primary_column"] == "geom"
    assert geo["columns"]["geom"]["encoding"] == "WKB"
    assert geo["columns"]["geom"]["bbox"] == [-61.0, 14.6, 2.35, 48.85]


def test_a_previous_parquet_is_replaced_not_appended(dirs: Path) -> None:
    ndjson(dirs / "split" / "part.geojson", ["a"])
    ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")
    ndjson(dirs / "split" / "part.geojson", ["b"])
    ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")
    assert [r[0] for r in read_parquet(dirs / "latest.parquet")] == ["b"]


def test_only_empty_files_is_an_error(dirs: Path) -> None:
    (dirs / "split" / "empty.geojson").write_text("")
    with pytest.raises(FileNotFoundError):
        ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")


def test_a_corrupt_line_fails_the_step_rather_than_dropping_the_feature(dirs: Path) -> None:
    ndjson(dirs / "split" / "part.geojson", ["a"])
    with (dirs / "split" / "part.geojson").open("a") as f:
        f.write('{"type": "Feature", "id": "cut off", "properties": {\n')
    with pytest.raises(duckdb.Error):
        ndg.convert_to_parquet(dirs / "split", dirs / "latest.parquet")
    assert not (dirs / "latest.parquet").exists()
    assert not (dirs / ".duckdb_temp").exists()
