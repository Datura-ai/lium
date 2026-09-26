"""A local HTTP stand-in for the provider portal: real sockets on 127.0.0.1, canned answers per route.

The CLI runs end to end against it (``--portal-url http://127.0.0.1:<port>``), transport included, so the
tests see the same Authorization header, status mapping and ``detail.code`` handling a real portal would get.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


class PortalStub:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        stub = self

        class _Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append(
                    {
                        "method": self.command,
                        "path": parts.path,
                        "query": parse_qs(parts.query),
                        "authorization": self.headers.get("Authorization"),
                        "json": json.loads(raw) if raw else None,
                    }
                )
                status, body = stub.routes.get((self.command, parts.path), (404, {"detail": "Not Found"}))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_DELETE = _answer

            def log_message(self, *args: Any) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self._thread.start()

    def route(self, method: str, path: str, body: Any, status: int = 200) -> None:
        self.routes[(method, path)] = (status, body)

    def calls(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(r["method"], r["path"]) for r in self.requests if method is None or r["method"] == method]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def closed_port_url() -> str:
    """A 127.0.0.1 URL nothing listens on (connection refused)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    return f"http://127.0.0.1:{port}"
