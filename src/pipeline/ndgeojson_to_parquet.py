"""
ATP GeoJSON to Parquet Conversion Pipeline.

This module handles the conversion of ATP (All The Places) GeoJSON data to Parquet format.

Background:
The ATP server's parquet generation is buggy because some GeoJSON files are too large
(~2 GB) for DuckDB to load directly. This module implements a multi-step workflow that
can handle GeoJSON files of any size.

Workflow:
1. convert_geojson_to_ndgeojson: Converts FeatureCollection GeoJSON to NDJSON format
   (one feature per line). This step is necessary because the original GeoJSON files
   are in FeatureCollection format which is harder to process in chunks.

2. split_ndgeojson: Splits large NDJSON files into smaller chunks (MAX_FILE_SIZE,
   see constants.py). This ensures that each file can be safely loaded into memory
   and processed by DuckDB without hitting memory limits.

3. convert_to_parquet: Converts the split NDJSON files to Parquet format using DuckDB.
   This is done in two phases:
   - Phase 1: Each NDJSON chunk is converted to a mini Parquet file in parallel
   - Phase 2: All mini Parquet files are merged into a single final Parquet file

The chunk size (MAX_FILE_SIZE) trades off two pressures:
- Small enough to keep each DuckDB task within its per-worker memory_limit.
- Large enough to keep the chunk count (and thus merge/filesystem overhead) low.
Note this is independent of `maximum_object_size` in _ndjson_to_parquet, which
caps a single JSON object (one feature), not the chunk file.

This approach, while more complex than direct conversion, ensures reliability
regardless of the input GeoJSON file sizes.
"""

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from src.pipeline.constants import (
    GEOJSON_DIR,
    MAX_FILE_SIZE,
    NDGEOJSON_DIR,
    SPLIT_DIR,
    WORKERS,
)

logger = logging.getLogger(__name__)


_NDJSON_COLS = "{id: 'VARCHAR', properties: 'JSON', geometry: 'JSON'}"


def convert_to_parquet(input_dir: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    files = [f for f in sorted(input_dir.glob("*.geojson")) if f.stat().st_size > 0]
    if not files:
        raise FileNotFoundError(f"No non-empty .geojson files found in {input_dir}")

    duck_temp = output_path.parent / ".duckdb_temp"
    duck_temp.mkdir(exist_ok=True)

    try:
        # Pre-install extension once to avoid races in threads
        with duckdb.connect() as con:
            con.install_extension("spatial")

        # Step 1: each NDJSON file → mini parquet (parallelized, max 16 MB input each)
        logger.info("Step 1/2 — converting %d NDJSON files to parquet...", len(files))

        def convert_one(args: tuple[int, Path]) -> Path:
            i, file_path = args
            out = duck_temp / f"part_{i:06d}.parquet"
            logger.info("[%d/%d] %s", i + 1, len(files), file_path.name)
            _ndjson_to_parquet(file_path, out)
            return out

        with ThreadPoolExecutor(max_workers=WORKERS) as executor:
            parts = list(executor.map(convert_one, enumerate(files)))

        parts = [p for p in parts if p.exists() and p.stat().st_size > 0]
        if not parts:
            raise RuntimeError("No parquet parts generated")

        # Step 2: merge all mini parquets → final parquet (parquet→parquet streams fine)
        logger.info("Step 2/2 — merging %d parquet parts...", len(parts))
        glob_parts = (duck_temp / "*.parquet").as_posix()

        # The spatial extension writes the GeoParquet metadata itself —
        # bbox, geometry types, the WKB encoding — so `geom` reads back as a
        # geometry, which is what import_atp relies on.
        with duckdb.connect() as con:
            con.load_extension("spatial")
            con.execute(f"SET threads={WORKERS}")
            con.execute("SET memory_limit='2GB'")
            # DuckDB, fed the paths this function chose.
            con.execute(f"""
                COPY (SELECT * FROM read_parquet('{glob_parts}'))
                TO '{output_path!s}'
                (FORMAT PARQUET, COMPRESSION 'ZSTD')
            """)  # noqa: S608
    finally:
        shutil.rmtree(duck_temp, ignore_errors=True)

    logger.info("Created %s", output_path)


def _ndjson_to_parquet(file_path: Path, out_path: Path) -> None:
    with duckdb.connect() as con:
        con.load_extension("spatial")
        con.execute("SET memory_limit='512MB'")
        con.execute("SET threads=1")
        con.execute(f"""
            COPY (
                SELECT id, properties, geom,
                    {{
                        'xmin': ST_XMin(geom),
                        'ymin': ST_YMin(geom),
                        'xmax': ST_XMax(geom),
                        'ymax': ST_YMax(geom)
                    }} AS bbox
                FROM (
                    SELECT
                        id,
                        properties,
                        ST_GeomFromGeoJSON(geometry::VARCHAR) AS geom
                    FROM read_json('{file_path.as_posix()}',
                        format='newline_delimited',
                        columns={_NDJSON_COLS},
                        maximum_object_size=16777216)
                    WHERE geometry IS NOT NULL
                )
            ) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION 'ZSTD')
        """)  # noqa: S608 — DuckDB, fed the paths the caller chose


def convert_geojson_to_ndgeojson(geojson_dir: Path, ndgeojson_dir: Path) -> None:
    """Convert FeatureCollection GeoJSON to NDJSON (one feature per line).

    Re-entrant: keeps any NDJSON already produced and deletes each source
    geojson as soon as its NDJSON is durably written. A crash can be resumed by
    re-running this step — only the un-deleted geojson files are reprocessed.
    """
    ndgeojson_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(geojson_dir.glob("*.geojson"))
    if not files:
        raise FileNotFoundError(f"No .geojson files in {geojson_dir}")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(_geojson_to_ndgeojson_single, f, ndgeojson_dir) for f in files]
        for fut in futures:
            fut.result()

    logger.info("Converted FC geojson to NDJSON")


def _geojson_to_ndgeojson_single(in_path: Path, ndgeojson_dir: Path) -> None:
    out_path = ndgeojson_dir / in_path.name

    # Already converted in a prior (partial) run — drop the redundant source.
    if out_path.exists():
        in_path.unlink()
        return

    if in_path.stat().st_size == 0:
        in_path.unlink()
        return

    # Write to a temp then atomically rename: out_path only ever exists once it
    # is complete, so "out exists" is a safe done-marker for resume.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    written = 0

    with in_path.open("rb") as f_in, tmp_path.open("wb") as f_out:
        first = True
        prev = None
        for line in f_in:
            if first:
                first = False
                continue  # skip FeatureCollection header
            if prev is not None:
                clean = prev.rstrip()
                if clean.endswith(b","):
                    clean = clean[:-1]
                if clean:
                    f_out.write(clean + b"\n")
                    written += 1
            prev = line
        # prev is the last line `]}` — skip it
        f_out.flush()
        os.fsync(f_out.fileno())

    if written == 0:
        tmp_path.unlink()
        in_path.unlink()
        logger.debug("Skipping %s: no features", in_path.name)
        return

    tmp_path.replace(out_path)  # durable, atomic on same filesystem
    in_path.unlink()  # source consumed — free it immediately


def split_ndgeojson(ndgeojson_dir: Path, split_dir: Path) -> None:
    """Split NDJSON files larger than MAX_FILE_SIZE; move smaller files as-is.

    Re-entrant: keeps any chunks already produced and consumes each source
    NDJSON in place (move or split+unlink), so a crash is resumed by simply
    re-running — only the un-consumed NDJSON files are reprocessed.
    """
    split_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(ndgeojson_dir.glob("*.geojson"))
    if not files:
        raise FileNotFoundError(f"No .geojson files in {ndgeojson_dir}")

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(_split_or_move_ndgeojson, f, split_dir) for f in files]
        for fut in futures:
            fut.result()

    logger.info("Split complete")


def _split_or_move_ndgeojson(in_path: Path, split_dir: Path) -> None:
    if in_path.stat().st_size <= MAX_FILE_SIZE:
        # move is atomic on same fs and deletes the source — re-entrant as-is.
        shutil.move(str(in_path), split_dir / in_path.name)
        return
    _split_ndgeojson_file(in_path, split_dir)


def _split_ndgeojson_file(in_path: Path, split_dir: Path) -> None:
    """Split a >MAX_FILE_SIZE NDJSON file into <=MAX_FILE_SIZE chunks named `{base}_{n}`.

    Reads one window (<=MAX_FILE_SIZE) at a time via seek instead of loading the whole
    file, so peak RAM is one window, not the full (~2 GB) file. The source is
    kept intact until every chunk is written, then unlinked.

    Crash-safe: because the source is untouched until the final unlink and the
    chunk boundaries are deterministic, a crash mid-split is recovered by simply
    re-running — the same `_1, _2, …` chunks are regenerated byte-identically
    (overwriting any partial leftovers), with no loss and no duplicates.
    """
    base_name = in_path.stem
    size = in_path.stat().st_size

    chunk_num = 0
    start = 0
    with in_path.open("rb") as f:
        while start < size:
            end = min(start + MAX_FILE_SIZE, size)
            if end < size:
                f.seek(start)
                window = f.read(end - start)
                nl = window.rfind(b"\n")
                if nl <= 0:
                    # A single line longer than the window: emitted whole,
                    # up to its own newline, rather than cut in the middle
                    # of the JSON object. Impossible in theory.
                    f.seek(end)
                    boundary = end + len(f.readline())
                else:
                    boundary = start + nl + 1
            else:
                boundary = size

            f.seek(start)
            chunk = f.read(boundary - start)
            chunk_num += 1
            (split_dir / f"{base_name}_{chunk_num}.geojson").write_bytes(chunk)
            start = boundary

    in_path.unlink()  # all chunks written — source consumed
    logger.debug("Split %s into %d chunks", in_path.name, chunk_num)


# Wrapper functions for pipeline runner (no parameters)
def convert_atp() -> None:
    """Step: Convert FeatureCollection GeoJSON to NDJSON."""
    if not GEOJSON_DIR.exists() or not any(GEOJSON_DIR.glob("*.geojson")):
        logger.info("No GeoJSON files found, skipping conversion")
        return
    convert_geojson_to_ndgeojson(GEOJSON_DIR, NDGEOJSON_DIR)


def split_atp() -> None:
    """Step: Split NDJSON files larger than MAX_FILE_SIZE."""
    if not NDGEOJSON_DIR.exists() or not any(NDGEOJSON_DIR.glob("*.geojson")):
        logger.info("No NDJSON files found, skipping split")
        return
    split_ndgeojson(NDGEOJSON_DIR, SPLIT_DIR)
