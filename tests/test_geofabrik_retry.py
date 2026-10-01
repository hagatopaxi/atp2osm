import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from src.pipeline.osm import _session  # pyright: ignore[reportPrivateUsage]

# Taken at import, before conftest's no_network replaces it: the server below
# listens on loopback, it is not the network.
_request = requests.Session.request


class _Geofabrik(BaseHTTPRequestHandler):
    """Slow to send the headers on the first request, or stalls mid-body."""

    requests_seen = 0
    stall_mid_body = False

    def do_GET(self) -> None:
        type(self).requests_seen += 1
        if not self.stall_mid_body and self.requests_seen == 1:
            time.sleep(1)
        self.send_response(200)
        self.send_header("Content-Length", "10")
        self.end_headers()
        self.wfile.write(b"12345")
        self.wfile.flush()
        if self.stall_mid_body:
            time.sleep(1)
        self.wfile.write(b"67890")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@pytest.fixture
def geofabrik(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setattr(requests.Session, "request", _request)
    _Geofabrik.requests_seen = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Geofabrik)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # The retries are mounted on https://; the test server speaks plain http.
    plain = _session.get_adapter("http://")
    _session.mount("http://", _session.get_adapter("https://"))
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    _session.mount("http://", plain)


def test_slow_headers_are_retried(geofabrik: str) -> None:
    _Geofabrik.stall_mid_body = False
    resp = _session.get(geofabrik, timeout=(1, 0.3))
    assert resp.content == b"1234567890"
    assert _Geofabrik.requests_seen == 2


def test_stall_mid_body_is_not_retried(geofabrik: str) -> None:
    _Geofabrik.stall_mid_body = True
    with (
        _session.get(geofabrik, stream=True, timeout=(1, 0.3)) as resp,
        pytest.raises(requests.ConnectionError),
    ):
        b"".join(resp.iter_content(chunk_size=5))
    assert _Geofabrik.requests_seen == 1
