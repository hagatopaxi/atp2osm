"""The streaming download every branch fetches its source with.

What matters is not the progress log but what is left on disk: a whole
file, or nothing — never a stump the next run would take for the source.
"""

import pytest
import requests

from src.utils import download_large_file


class _Stream:
    def __init__(self, chunks, length=None, fail_after=None):
        self.chunks, self.fail_after = chunks, fail_after
        self.headers = {"Content-Length": str(length)} if length is not None else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for i, chunk in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise requests.ConnectionError("reset by peer")
            yield chunk


class _Http:
    def __init__(self, stream):
        self.stream, self.calls = stream, []

    def get(self, url, stream=None, timeout=None):
        self.calls.append((url, stream, timeout))
        return self.stream


def test_the_body_is_written_whole(tmp_path):
    http = _Http(_Stream([b"abc", b"", b"def"], length=6))  # keep-alive chunk in between
    dest = tmp_path / "osm" / "x.pbf"

    download_large_file("https://x.example/x.pbf", dest, session=http)

    assert dest.read_bytes() == b"abcdef"
    (url, stream, timeout), = http.calls
    assert stream is True
    assert timeout[1] >= 60  # a slow source is not a dead one


def test_a_body_without_a_length_is_written_too(tmp_path):
    dest = tmp_path / "x.bin"
    download_large_file("https://x.example/x", dest, session=_Http(_Stream([b"ab"])))
    assert dest.read_bytes() == b"ab"


def test_an_empty_body_leaves_no_file(tmp_path):
    dest = tmp_path / "x.pbf"
    with pytest.raises(ValueError, match="empty"):
        download_large_file("https://x.example/x.pbf", dest, session=_Http(_Stream([])))
    assert not dest.exists()


def test_a_connection_lost_mid_stream_leaves_no_stump(tmp_path):
    dest = tmp_path / "x.pbf"
    with pytest.raises(requests.ConnectionError):
        download_large_file(
            "https://x.example/x.pbf", dest,
            session=_Http(_Stream([b"abc", b"def"], length=6, fail_after=1)),
        )
    assert not dest.exists()


def test_the_progress_is_logged_along_the_way(tmp_path, caplog, monkeypatch):
    import src.utils as utils

    clock = iter([0, 0, 20, 20, 40, 40, 41])
    monkeypatch.setattr(utils.time, "time", lambda: next(clock))
    caplog.set_level("INFO", logger="src.utils")
    download_large_file(
        "https://x.example/x.pbf", tmp_path / "x.pbf",
        session=_Http(_Stream([b"a", b"b"], length=2)), progress_interval=15,
    )
    assert any("%" in r.message for r in caplog.records)
    assert any("complete" in r.message for r in caplog.records)
