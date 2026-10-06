"""`Lium.logs` uses the same HTTP path as every other call.

It used to call `requests.get` itself with a second copy of the status-to-
exception mapping, which had drifted: raw `response.text` in messages, no
retry while opening the stream. It now goes through `_request` and shares
`_raise_for_status`.
"""

import pytest
import requests

from lium.sdk import (
    Config,
    Lium,
    LiumAuthError,
    LiumError,
    LiumNotFoundError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
)
from lium.sdk import client as client_module
from lium.sdk import utils as sdk_utils


class _Response:
    def __init__(self, status_code=200, lines=(), payload=None, text=""):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._lines = list(lines)
        self._payload = payload
        self.text = text
        self.closed = False

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload

    def iter_lines(self):
        yield from self._lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    return Lium(Config(api_key="test"))


def _script(monkeypatch, *outcomes):
    calls = []
    queue = list(outcomes)

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client_module.requests, "request", fake_request)
    return calls


def test_logs_streams_the_pod_endpoint(client, monkeypatch):
    calls = _script(monkeypatch, _Response(lines=[b"line 1", b"", b"line 2"]))

    assert list(client.logs("pod-1", tail=5)) == [b"line 1", b"line 2"]

    [call] = calls
    assert call["method"] == "GET"
    assert call["url"].endswith("/pods/pod-1/logs")
    assert call["params"] == {"tail": 5, "follow": "false"}
    assert call["stream"] is True
    assert call["timeout"] == 30
    assert call["headers"]["X-API-KEY"] == "test"


def test_the_pod_id_is_quoted_in_the_path(client, monkeypatch):
    calls = _script(monkeypatch, _Response())

    list(client.logs("../users/me?x", tail=1))

    assert calls[0]["url"].endswith("/pods/..%2Fusers%2Fme%3Fx/logs")


def test_follow_has_no_timeout(client, monkeypatch):
    calls = _script(monkeypatch, _Response())

    list(client.logs("pod-1", follow=True))

    assert calls[0]["timeout"] is None
    assert calls[0]["params"]["follow"] == "true"


def test_the_response_is_closed_after_iteration(client, monkeypatch):
    response = _Response(lines=[b"x"])
    _script(monkeypatch, response)

    list(client.logs("pod-1"))

    assert response.closed


def test_403_uses_the_parsed_detail_like_every_other_call(client, monkeypatch):
    _script(monkeypatch, _Response(403, payload={"detail": "User is not verified"}, text='{"detail": "..."}'))

    with pytest.raises(LiumPermissionError, match="Permission denied: User is not verified"):
        list(client.logs("pod-1"))


def test_a_failed_response_is_closed_before_the_exception_leaves(client, monkeypatch):
    """With stream=True the body is never read, so the socket would live until GC."""
    responses = [_Response(403, payload={"detail": "no"}), _Response(503), _Response(503), _Response(503)]
    _script(monkeypatch, *responses)

    with pytest.raises(LiumPermissionError):
        list(client.logs("pod-1"))
    with pytest.raises(LiumServerError):
        list(client.logs("pod-1"))

    assert all(response.closed for response in responses)


def test_404_still_names_the_pod(client, monkeypatch):
    _script(monkeypatch, _Response(404, payload={"detail": "Not found"}))

    with pytest.raises(LiumNotFoundError, match="Pod not found: pod-1"):
        list(client.logs("pod-1"))


def test_a_5xx_while_opening_the_stream_is_retried(client, monkeypatch):
    calls = _script(monkeypatch, _Response(502), requests.ConnectionError("reset"), _Response(lines=[b"ok"]))

    assert list(client.logs("pod-1")) == [b"ok"]
    assert len(calls) == 3


def test_persistent_failure_raises_after_three_attempts(client, monkeypatch):
    calls = _script(monkeypatch, _Response(503), _Response(503), _Response(503))

    with pytest.raises(LiumServerError):
        list(client.logs("pod-1"))

    assert len(calls) == 3


@pytest.mark.parametrize("status, exc", [
    (401, LiumAuthError),
    (403, LiumPermissionError),
    (404, LiumNotFoundError),
    (429, LiumRateLimitError),
    (500, LiumServerError),
    (599, LiumServerError),
    (418, LiumError),
])
def test_raise_for_status_maps_each_status_as_before(status, exc):
    with pytest.raises(exc):
        Lium._raise_for_status(_Response(status, text="body"))


def test_raise_for_status_lets_success_through():
    Lium._raise_for_status(_Response(204))


def test_request_default_timeout_is_still_thirty_seconds(client, monkeypatch):
    calls = _script(monkeypatch, _Response(payload={}))

    client._request("GET", "/pods")

    assert calls[0]["timeout"] == 30
