from __future__ import annotations

import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"loopback-ok")

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="function")
def local_http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def external_service():
    # Expected to fail in the network-isolated control run.
    with socket.create_connection(("www.iana.org", 443), timeout=5):
        yield "external connection unexpectedly allowed"


@pytest.fixture(scope="module", params=["alpha", "beta"])
def parametrized_resource(request):
    yield request.param
