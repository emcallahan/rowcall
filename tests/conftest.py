"""A tiny real HTTP server for tests, so sending is tested end to end without mocks."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.received.append(
            {"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body}
        )

        extra_headers = {}
        if self.path.startswith("/flaky"):
            # Fails with 429 the first time each path is called, then works.
            seen = self.server.seen
            seen[self.path] = seen.get(self.path, 0) + 1
            if seen[self.path] == 1:
                status, payload = 429, {"error": "slow down"}
                extra_headers["Retry-After"] = "2"
            else:
                status, payload = 201, {"ok": True}
        elif self.path.startswith("/busy"):
            status, payload = 503, {"error": "busy"}
            extra_headers["Retry-After"] = "600"
        elif self.path.startswith("/gateway"):
            status, payload = 502, {"error": "bad gateway"}
        elif self.path.startswith("/fail"):
            status, payload = 500, {"error": "something broke"}
        elif self.path.startswith("/echo-auth"):
            status, payload = 401, {"error": "bad token", "got": self.headers.get("Authorization")}
        else:
            status, payload = 201, {"id": len(self.server.received), "received": body}

        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

    def log_message(self, *args):
        pass  # keep test output quiet


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.received = []
    httpd.seen = {}
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()
    httpd.server_close()
