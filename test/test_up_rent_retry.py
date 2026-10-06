"""One `lium up` must create one pod.

`_request` retried every transient failure, including a timed-out POST to the
rent endpoint. A rent that the server completed but whose response never
arrived was therefore sent again, and the account ended up with two pods for
one command.
"""

import uuid
from types import SimpleNamespace

import pytest
import requests

from lium.sdk import Config, Lium, LiumServerError
from lium.sdk import client as client_module
from lium.sdk import utils as sdk_utils

EXECUTOR_ID = "executor-1"
POD_NAME = "brave-orbit-b9"


class _Resp:
    ok = True
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _ServerError:
    ok = False
    status_code = 502
    text = "bad gateway"

    def json(self):
        raise ValueError("no body")


def _pod(name=POD_NAME, executor_id=EXECUTOR_ID, pod_id="pod-uuid-1"):
    return SimpleNamespace(
        id=pod_id, name=name, status="PENDING", huid="eager-wolf-aa", ssh_cmd=None,
        executor=SimpleNamespace(id=executor_id) if executor_id else None,
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(client_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    lium = Lium(Config(api_key="test"))
    monkeypatch.setattr(lium, "get_executor", lambda executor_id: SimpleNamespace(id=EXECUTOR_ID))
    monkeypatch.setattr(lium, "_ensure_ssh_keys_registered", lambda *a, **k: None)
    return lium


def _rent(client, *, pods_after_failure, pods_before=()):
    """Rent once; `outcomes` decides what each POST does.

    `ps` answers with `pods_before` until the first POST goes out (the snapshot
    `up` takes before renting) and with `pods_after_failure` from then on.
    """
    calls = []

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append({"method": method, "url": url, "headers": headers, "json": kwargs.get("json")})
        outcome = _rent.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    client.ps = lambda: list(pods_after_failure) if calls else list(pods_before)
    _rent.calls = calls
    return fake_request


def test_rent_sends_an_idempotency_key(client, monkeypatch):
    _rent.outcomes = [_Resp({"id": "pod-uuid-1", "name": POD_NAME})]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["ssh-ed25519 AAA"])

    [call] = _rent.calls
    assert call["url"].endswith(f"/executors/{EXECUTOR_ID}/rent")
    uuid.UUID(call["headers"]["Idempotency-Key"])  # well-formed
    assert call["headers"]["X-API-KEY"] == "test"  # the usual headers are still there


def test_the_real_rent_payload_is_resolved_by_pod_id(client, monkeypatch):
    """The rent route answers {"success": true, "pod_id": ...}, not a pod record, so
    every real rent goes through the read-back; the exact id wins over the name."""
    stale = _pod(pod_id="pod-uuid-stale")  # same name, same node, older
    _rent.outcomes = [_Resp({"success": True, "pod_id": "pod-uuid-new"})]
    monkeypatch.setattr(
        client_module.requests, "request",
        _rent(client, pods_before=[stale], pods_after_failure=[stale, _pod(pod_id="pod-uuid-new")]),
    )

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-new"
    assert result["executor_id"] == EXECUTOR_ID
    assert len(_rent.calls) == 1


def test_a_timed_out_rent_is_not_repeated_when_the_pod_exists(client, monkeypatch):
    """The pod the server created during the timeout is the result; no second POST."""
    _rent.outcomes = [requests.Timeout("read timed out")]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[_pod()]))

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-1"
    assert result["executor_id"] == EXECUTOR_ID
    assert len(_rent.calls) == 1


def test_a_timed_out_rent_is_retried_once_with_the_same_key_when_nothing_appeared(client, monkeypatch):
    _rent.outcomes = [requests.Timeout("read timed out"), _Resp({"id": "pod-uuid-2", "name": POD_NAME})]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-2"
    assert len(_rent.calls) == 2
    first, second = _rent.calls
    assert first["headers"]["Idempotency-Key"] == second["headers"]["Idempotency-Key"]
    assert first["json"] == second["json"]


def test_a_5xx_on_rent_looks_for_the_pod_before_retrying(client, monkeypatch):
    """A gateway error after the server committed the rent is the same duplicate risk."""
    _rent.outcomes = [_ServerError()]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[_pod()]))

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-1"
    assert len(_rent.calls) == 1


def test_a_pod_with_the_same_name_on_another_node_is_not_mistaken_for_ours(client, monkeypatch):
    _rent.outcomes = [requests.Timeout("read timed out"), _Resp({"id": "pod-uuid-2", "name": POD_NAME})]
    other_node_pod = _pod(executor_id="executor-other")
    monkeypatch.setattr(
        client_module.requests, "request", _rent(client, pods_after_failure=[other_node_pod])
    )

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-2"
    assert len(_rent.calls) == 2


def test_a_stale_same_name_pod_on_the_same_node_is_not_mistaken_for_ours(client, monkeypatch):
    """GPU splitting puts several pods on one node and the default name is the node huid,
    so a pod that existed before the rent must not be handed back for a rent the server
    never got: the request is sent once more instead."""
    stale = _pod(pod_id="pod-uuid-stale")
    _rent.outcomes = [requests.Timeout("read timed out"), _Resp({"id": "pod-uuid-2", "name": POD_NAME})]
    monkeypatch.setattr(
        client_module.requests, "request",
        _rent(client, pods_before=[stale], pods_after_failure=[stale]),
    )

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-2"
    assert len(_rent.calls) == 2


def test_the_pod_that_appeared_next_to_a_stale_one_is_ours(client, monkeypatch):
    stale = _pod(pod_id="pod-uuid-stale")
    _rent.outcomes = [requests.Timeout("read timed out")]
    monkeypatch.setattr(
        client_module.requests, "request",
        _rent(client, pods_before=[stale], pods_after_failure=[stale, _pod(pod_id="pod-uuid-new")]),
    )

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-new"
    assert len(_rent.calls) == 1


def test_a_failed_snapshot_does_not_fail_the_rent(client, monkeypatch):
    """Listing pods before the rent is best effort: if it fails, `up` still rents."""
    _rent.outcomes = [_Resp({"id": "pod-uuid-1", "name": POD_NAME})]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    def failing_ps():
        raise LiumServerError("listing down")

    client.ps = failing_ps

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-1"
    assert len(_rent.calls) == 1


def test_a_listing_that_fails_too_still_leads_to_the_second_post(client, monkeypatch):
    """The network that failed the POST fails ps() the same way; "could not look" is
    not "no pod", and the second POST the design promises must still go out."""
    _rent.outcomes = [requests.Timeout("read timed out"), _Resp({"id": "pod-uuid-2", "name": POD_NAME})]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    def failing_ps():
        raise requests.ConnectionError("network down")

    client.ps = failing_ps

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-2"
    assert len(_rent.calls) == 2


def test_a_pod_id_from_the_api_is_returned_even_when_the_listing_is_down(client, monkeypatch):
    """After a successful POST the id is the truth; a failing ps() must not turn it into an error."""
    _rent.outcomes = [_Resp({"success": True, "pod_id": "pod-uuid-9"})]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    def failing_ps():
        raise LiumServerError("Server error: 503")

    client.ps = failing_ps

    result = client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert result["id"] == "pod-uuid-9" and result["executor_id"] == EXECUTOR_ID
    assert len(_rent.calls) == 1


def test_a_second_failure_is_raised_not_retried_again(client, monkeypatch):
    _rent.outcomes = [requests.Timeout("first"), requests.Timeout("second")]
    monkeypatch.setattr(client_module.requests, "request", _rent(client, pods_after_failure=[]))

    with pytest.raises(requests.Timeout, match="second"):
        client.up(executor_id=EXECUTOR_ID, name=POD_NAME, template_id="tpl-1", ssh_keys=["k"])

    assert len(_rent.calls) == 2


def test_request_without_retry_fails_on_the_first_server_error(monkeypatch):
    attempts = []

    def fake_request(*args, **kwargs):
        attempts.append(1)
        return _ServerError()

    monkeypatch.setattr(sdk_utils.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(client_module.requests, "request", fake_request)
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumServerError):
        client._request("POST", "/executors/x/rent", retry=False)
    assert len(attempts) == 1

    with pytest.raises(LiumServerError):
        client._request("GET", "/pods")
    assert len(attempts) == 1 + 3  # idempotent calls keep their three attempts
