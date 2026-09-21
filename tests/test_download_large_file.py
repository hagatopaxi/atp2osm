"""The streaming download every branch fetches its source with.

What matters is not the progress log but what is left on disk: a whole
file, or nothing — never a stump the next run would take for the source.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Self, cast

import pytest
import requests

from src.utils import download_large_file


class _Stream:
    def __init__(
        self, chunks: list[bytes], length: int | None = None, fail_after: int | None = None
    ) -> None:
        self.chunks, self.fail_after = chunks, fail_after
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def raise_for_status(self) -> None:
        pass

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise requests.ConnectionError("reset by peer")
            yield chunk


class _Http:
    """The one method of a requests.Session the download calls."""

    def __init__(self, stream: _Stream) -> None:
        self.stream = stream
        self.calls: list[tuple[str, bool | None, tuple[float, float] | None]] = []

    def get(
        self, url: str, stream: bool | None = None, timeout: tuple[float, float] | None = None
    ) -> _Stream:
        self.calls.append((url, stream, timeout))
        return self.stream

    @property
    def session(self) -> requests.Session:
        return cast("requests.Session", self)


def test_the_body_is_written_whole(tmp_path: Path) -> None:
    http = _Http(_Stream([b"abc", b"", b"def"], length=6))  # keep-alive chunk in between
    dest = tmp_path / "osm" / "x.pbf"

    download_large_file("https://x.example/x.pbf", dest, session=http.session)

    assert dest.read_bytes() == b"abcdef"
    ((_url, stream, timeout),) = http.calls
    assert stream is True
    assert timeout is not None
    assert timeout[1] >= 60  # a slow source is not a dead one


def test_a_body_without_a_length_is_written_too(tmp_path: Path) -> None:
    dest = tmp_path / "x.bin"
    download_large_file("https://x.example/x", dest, session=_Http(_Stream([b"ab"])).session)
    assert dest.read_bytes() == b"ab"


def test_an_empty_body_leaves_no_file(tmp_path: Path) -> None:
    dest = tmp_path / "x.pbf"
    with pytest.raises(ValueError, match="empty"):
        download_large_file("https://x.example/x.pbf", dest, session=_Http(_Stream([])).session)
    assert not dest.exists()


def test_a_connection_lost_mid_stream_leaves_no_stump(tmp_path: Path) -> None:
    dest = tmp_path / "x.pbf"
    with pytest.raises(requests.ConnectionError):
        download_large_file(
            "https://x.example/x.pbf",
            dest,
            session=_Http(_Stream([b"abc", b"def"], length=6, fail_after=1)).session,
        )
    assert not dest.exists()


def test_the_progress_is_logged_along_the_way(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import utils

    clock = iter([0, 0, 20, 20, 40, 40, 41])
    monkeypatch.setattr(utils.time, "time", lambda: next(clock))
    caplog.set_level("INFO", logger="src.utils")
    download_large_file(
        "https://x.example/x.pbf",
        tmp_path / "x.pbf",
        session=_Http(_Stream([b"a", b"b"], length=2)).session,
        progress_interval=15,
    )
    assert any("%" in r.message for r in caplog.records)
    assert any("complete" in r.message for r in caplog.records)
