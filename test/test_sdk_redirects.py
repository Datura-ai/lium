"""A redirect never carries the API key (or a body) to another origin.

`_request_once` used to call `requests.request` with its default redirect
handling. `requests` drops only the `Authorization` header when a redirect
changes host; the renter key travels in `X-API-KEY`, so a 3xx from the API
host with a `Location` elsewhere re-sent the key verbatim, and a 307/308
re-sent the body too (ticket-0260 report 7). The client now follows a
redirect itself, and only to the same scheme, host and port.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from lium.sdk import Config, Lium, LiumError
from lium.sdk import client as client_module

KEY = "renter-key-for-this-test"


class _Ok:
    ok = True
    status_code = 200
    headers: dict = {}

    def json(self):
        return {}


class _Redirect:
    ok = True  # `requests` counts 3xx as ok; the old code handed such a response back as a success

    def __init__(self, status_code: int, location: str):
        self.status_code = status_code
        self.headers = {"Location": location}
        self.closed = False

    def close(self):
        self.closed = True

    def json(self):
        return {}


@pytest.fixture
def client():
    return Lium(Config(api_key=KEY, base_url="https://api.example/api"))


def _script(monkeypatch, *responses):
    """Answer each `requests.request` call from the list; return the call log (method, url, kwargs)."""
    calls = []
    queue = list(responses)

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "headers": headers, **kwargs})
        return queue.pop(0) if queue else _Ok()

    monkeypatch.setattr(client_module.requests, "request", fake_request)
    return calls


def test_the_client_follows_redirects_itself(client, monkeypatch):
    calls = _script(monkeypatch, _Ok())
    client._request("GET", "/pods")
    assert calls[0]["allow_redirects"] is False


def test_a_redirect_to_another_host_is_refused_and_the_key_is_not_sent_there(client, monkeypatch):
    calls = _script(monkeypatch, _Redirect(302, "https://evil.example/collect"))
    with pytest.raises(LiumError) as exc:
        client._request("GET", "/pods")
    assert len(calls) == 1, "nothing was sent to the redirect target"
    assert "evil.example" in str(exc.value) and KEY not in str(exc.value)


def test_a_307_to_another_host_does_not_replay_the_body(client, monkeypatch):
    body = {"template_id": "t", "hostname": "h"}
    calls = _script(monkeypatch, _Redirect(307, "https://evil.example/pods"))
    with pytest.raises(LiumError):
        client._request("POST", "/pods", json=body, retry=False)
    assert [c["url"] for c in calls] == ["https://api.example/api/pods"]


def test_a_downgrade_to_http_on_the_same_host_is_refused(client, monkeypatch):
    # what https://lium.io/api/pods/ answered on 16 Sep 2026: 307 to http://lium.io/api/pods
    calls = _script(monkeypatch, _Redirect(307, "http://api.example/api/pods"))
    with pytest.raises(LiumError):
        client._request("GET", "/pods/")
    assert len(calls) == 1


def test_a_same_origin_307_keeps_method_body_and_key(client, monkeypatch):
    body = {"template_id": "t"}
    hop = _Redirect(307, "/api/pods")
    calls = _script(monkeypatch, hop, _Ok())
    client._request("POST", "/pods/", json=body, retry=False)
    assert [c["url"] for c in calls] == ["https://api.example/api/pods/", "https://api.example/api/pods"]
    assert calls[1]["method"] == "POST" and calls[1]["json"] == body
    assert calls[1]["headers"]["X-API-KEY"] == KEY
    assert hop.closed, "the intermediate response is closed before the next hop"


def test_a_same_origin_302_after_a_post_becomes_a_get_without_a_body(client, monkeypatch):
    calls = _script(monkeypatch, _Redirect(302, "https://api.example:443/api/pods"), _Ok())
    client._request("POST", "/pods/", json={"x": 1}, retry=False)
    assert calls[1]["method"] == "GET" and "json" not in calls[1]


def test_a_same_origin_301_after_a_put_keeps_the_put(client, monkeypatch):
    """`requests` rewrites only a POST on a 301; a PUT stays a PUT (this client keeps its body too)."""
    calls = _script(monkeypatch, _Redirect(301, "/api/pods/p1"), _Ok())
    client._request("PUT", "/pods/p1/", json={"x": 1}, retry=False)
    assert calls[1]["method"] == "PUT" and calls[1]["json"] == {"x": 1}


def test_the_query_is_not_added_twice_on_a_followed_redirect(client, monkeypatch):
    """The `Location` already carries the query string; re-sending `params` would double it."""
    calls = _script(monkeypatch, _Redirect(307, "/api/executors?gpu=H100"), _Ok())
    client._request("GET", "/executors/", params={"gpu": "H100"})
    assert calls[0]["params"] == {"gpu": "H100"}
    assert calls[1]["url"] == "https://api.example/api/executors?gpu=H100" and "params" not in calls[1]


def test_a_location_that_is_not_a_url_is_a_lium_error(client, monkeypatch):
    calls = _script(monkeypatch, _Redirect(302, "http://[bad/"))
    with pytest.raises(LiumError):
        client._request("GET", "/pods")
    assert len(calls) == 1


def test_a_redirect_loop_stops(client, monkeypatch):
    calls = _script(monkeypatch, *[_Redirect(302, "/api/pods") for _ in range(20)])
    with pytest.raises(LiumError, match="more than"):
        client._request("GET", "/pods")
    assert len(calls) == client_module._MAX_REDIRECTS + 1


def test_a_refused_redirect_is_not_retried(client, monkeypatch):
    calls = _script(monkeypatch, *[_Redirect(302, "https://evil.example/") for _ in range(3)])
    with pytest.raises(LiumError):
        client._request("GET", "/pods")  # GET would be retried three times were this a transient error
    assert len(calls) == 1


# --- one real round trip: the mocks above script `requests`; this shows what `requests` itself does ---


def _serve(handler):
    server = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_over_a_socket_the_other_host_never_sees_the_key():
    """Two loopback servers on different ports are two origins. Before the fix the second one
    received `X-API-KEY` on the 302 and the JSON body on the 307."""
    received = []

    class Collector(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _take(self):
            n = int(self.headers.get("Content-Length") or 0)
            received.append((self.command, dict(self.headers), self.rfile.read(n).decode() if n else ""))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        do_GET = do_POST = _take

    collector = _serve(Collector)
    target = f"http://127.0.0.1:{collector.server_port}"

    class Redirector(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"{target}/got-it")
            self.end_headers()

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(307)
            self.send_header("Location", f"{target}/got-it")
            self.end_headers()

    redirector = _serve(Redirector)
    try:
        client = Lium(Config(api_key=KEY, base_url=f"http://127.0.0.1:{redirector.server_port}"))
        with pytest.raises(LiumError):
            client._request("GET", "/pods")
        with pytest.raises(LiumError):
            client._request("POST", "/pods", json={"secret": "body"}, retry=False)
    finally:
        redirector.shutdown()
        collector.shutdown()
    assert received == []
