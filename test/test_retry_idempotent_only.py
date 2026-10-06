"""Only a request that is safe to repeat is repeated.

`_request` used to retry every transient failure three times, whatever the
method. After the rent fix opted the pod-create POST out, every other POST,
PUT and DELETE still went out again after a timeout — a second template,
volume, backup or invoice when the first one had in fact been applied. The
default now follows the HTTP method: a mutating call is repeated only after a
429 (the server declined to run it, so nothing was applied); callers can still
override it per call.
"""

from types import SimpleNamespace

import pytest
import requests

from lium.sdk import Config, Lium, LiumRateLimitError, LiumServerError
from lium.sdk import client as client_module
from lium.sdk import utils as sdk_utils
from lium.sdk.client import IDEMPOTENT_METHODS


class _Ok:
    ok = True
    status_code = 200

    def __init__(self, data=None):
        self._data = data or {}

    def json(self):
        return self._data


class _ServerError:
    ok = False
    status_code = 502
    text = "bad gateway"

    def json(self):
        raise ValueError("no body")


class _RateLimited:
    ok = False
    status_code = 429
    text = "slow down"

    def json(self):
        raise ValueError("no body")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    return Lium(Config(api_key="test"))


def _outcomes(monkeypatch, *outcomes):
    """Script what `requests.request` does on each call; return the call log."""
    calls = []
    queue = list(outcomes)

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "json": kwargs.get("json")})
        outcome = queue.pop(0) if queue else _ServerError()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(client_module.requests, "request", fake_request)
    return calls


def test_the_idempotent_set_is_the_read_only_methods():
    assert IDEMPOTENT_METHODS == {"GET", "HEAD", "OPTIONS"}


@pytest.mark.parametrize("failure", [_ServerError(), requests.ConnectionError("reset")])
def test_get_keeps_three_attempts(client, monkeypatch, failure):
    calls = _outcomes(monkeypatch, failure, failure, failure)

    with pytest.raises((LiumServerError, requests.RequestException)):
        client._request("GET", "/pods")

    assert len(calls) == 3


def test_get_succeeds_on_a_later_attempt(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError(), _Ok({"fine": True}))

    assert client._request("GET", "/pods").json() == {"fine": True}
    assert len(calls) == 2


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_are_sent_once_on_a_server_error(client, monkeypatch, method):
    calls = _outcomes(monkeypatch, _ServerError())

    with pytest.raises(LiumServerError):
        client._request(method, "/things")

    assert len(calls) == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_are_sent_once_on_a_timeout(client, monkeypatch, method):
    calls = _outcomes(monkeypatch, requests.Timeout("read timed out"))

    with pytest.raises(requests.Timeout):
        client._request(method, "/things")

    assert len(calls) == 1


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_mutating_methods_back_off_and_repeat_after_a_429(client, monkeypatch, method):
    # a 429 is an answer instead of an execution: repeating cannot create a duplicate
    calls = _outcomes(monkeypatch, _RateLimited(), _Ok({"id": "t-1"}))

    assert client._request(method, "/things").json() == {"id": "t-1"}
    assert len(calls) == 2


def test_a_mutating_call_gives_up_after_three_429s(client, monkeypatch):
    calls = _outcomes(monkeypatch, _RateLimited(), _RateLimited(), _RateLimited())

    with pytest.raises(LiumRateLimitError):
        client._request("POST", "/things")

    assert len(calls) == 3


def test_a_429_followed_by_a_server_error_is_not_repeated_further(client, monkeypatch):
    calls = _outcomes(monkeypatch, _RateLimited(), _ServerError())

    with pytest.raises(LiumServerError):
        client._request("DELETE", "/things/1")

    assert len(calls) == 2


def test_retry_false_sends_once_even_on_a_429(client, monkeypatch):
    calls = _outcomes(monkeypatch, _RateLimited(), _Ok())

    with pytest.raises(LiumRateLimitError):
        client._request("POST", "/pods", retry=False)

    assert len(calls) == 1


def test_method_case_does_not_matter(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError())

    with pytest.raises(LiumServerError):
        client._request("post", "/things")

    assert len(calls) == 1


def test_a_successful_mutating_call_is_returned_unchanged(client, monkeypatch):
    calls = _outcomes(monkeypatch, _Ok({"id": "v-1"}))

    assert client._request("POST", "/volumes", json={"name": "x"}).json() == {"id": "v-1"}
    assert calls == [{"method": "POST", "url": f"{client.config.base_url}/volumes", "json": {"name": "x"}}]


def test_retry_true_forces_retries_on_a_post(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError(), _Ok())

    client._request("POST", "/pods/x/cancel-something", retry=True)

    assert len(calls) == 2


def test_retry_false_disables_retries_on_a_get(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError())

    with pytest.raises(LiumServerError):
        client._request("GET", "/pods", retry=False)

    assert len(calls) == 1


# --- through the public methods that used to duplicate -------------------------------------

def test_volume_create_sends_one_request_when_the_server_fails(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError())

    with pytest.raises(LiumServerError):
        client.volume_create("datasets")

    assert [c["method"] for c in calls] == ["POST"]


def test_create_template_sends_one_request_when_the_server_fails(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError())

    with pytest.raises(LiumServerError):
        client.create_template("mine", "daturaai/pytorch")

    assert [c["method"] for c in calls] == ["POST"]


def test_rm_sends_one_request_when_the_server_fails(client, monkeypatch):
    calls = _outcomes(monkeypatch, requests.Timeout("read timed out"))

    with pytest.raises(requests.Timeout):
        client.rm(SimpleNamespace(id="pod-uuid-1"))

    assert [c["method"] for c in calls] == ["DELETE"]


# --- idempotent payloads that keep the old behaviour --------------------------------------

def test_schedule_termination_is_repeated_after_a_server_error(client, monkeypatch):
    # one 5xx after `lium up --ttl` must not leave the pod without its auto-stop
    calls = _outcomes(monkeypatch, _ServerError(), _Ok({"removal_scheduled_at": "2026-09-09T00:00:00Z"}))

    result = client.schedule_termination(SimpleNamespace(id="pod-uuid-1"), termination_time="2026-09-09T00:00:00Z")

    assert result == {"removal_scheduled_at": "2026-09-09T00:00:00Z"}
    assert [c["method"] for c in calls] == ["POST", "POST"]
    assert calls[0]["json"] == calls[1]["json"] == {"removal_scheduled_at": "2026-09-09T00:00:00Z"}


def test_backup_cancel_is_repeated_after_a_timeout(client, monkeypatch):
    calls = _outcomes(monkeypatch, requests.Timeout("read timed out"), _Ok({"status": "cancelling"}))

    assert client.backup_cancel("backup-1") == {"status": "cancelling"}
    assert [c["method"] for c in calls] == ["POST", "POST"]


def test_restore_cancel_is_repeated_after_a_server_error(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError(), _Ok({"status": "cancelling"}))

    assert client.restore_cancel("restore-1") == {"status": "cancelling"}
    assert [c["method"] for c in calls] == ["POST", "POST"]


def test_ps_still_retries(client, monkeypatch):
    calls = _outcomes(monkeypatch, _ServerError(), _Ok([]))

    assert client.ps() == []
    assert [c["method"] for c in calls] == ["GET", "GET"]
