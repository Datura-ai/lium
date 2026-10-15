"""An agent can rate a pod's node in the same call that removes the pod."""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.rm import command as rm_module
from lium.sdk import Config, Lium, LiumError


class _Response:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


def _client(monkeypatch):
    client = Lium(Config(api_key="test"))
    calls = []

    def fake_request(method, endpoint, json=None, **kwargs):
        calls.append((method, endpoint, json))
        return _Response({"success": True})

    monkeypatch.setattr(client, "_request", fake_request)
    monkeypatch.setattr("lium.sdk.client.forget_host_key", lambda pod: None)
    return client, calls


@pytest.mark.parametrize("outcomes", [
    ("ok",),
    ("rejected",),
    ("rejected", "ok"),
    ("ok", "in_progress"),
])
def test_down_sends_feedback_in_the_delete(monkeypatch, outcomes):
    # a retried removal must not POST a second rating: the backend records DELETE-body feedback once
    client, calls = _client(monkeypatch)
    pending = iter(outcomes)

    def request(method, endpoint, json=None, **kwargs):
        calls.append((method, endpoint, json))
        if method == "DELETE":
            outcome = next(pending)
            if outcome == "rejected":
                raise LiumError("pod is not running")
        return _Response({"success": True})

    monkeypatch.setattr(client, "_request", request)
    for _ in outcomes:
        try:
            client.down(SimpleNamespace(id="pod-1"), rating=1)
        except LiumError:
            pass

    payload = {"feedback_text": "", "rating": 1, "reason": "unrent"}
    assert calls == [("DELETE", "/pods/pod-1", payload)] * len(outcomes)


def test_down_without_feedback_only_deletes(monkeypatch):
    client, calls = _client(monkeypatch)

    client.down(SimpleNamespace(id="pod-1"))

    assert calls == [("DELETE", "/pods/pod-1", None)]


def test_down_refuses_an_out_of_range_rating_before_deleting(monkeypatch):
    client, calls = _client(monkeypatch)

    with pytest.raises(ValueError):
        client.down(SimpleNamespace(id="pod-1"), rating=9)

    assert calls == []


def _run_rm(monkeypatch, *args):
    pod = SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name="train-pod", executor=None, created_at=None)
    calls = []

    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)
        config = SimpleNamespace(workspace=None, workspace_id=None, workspace_explicit=False)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [pod]

        def rm(self, pod, **feedback):
            calls.append(feedback)
            return {"success": True}

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["rm", "train-pod", "-y", *args]), calls


def test_rm_passes_feedback_and_rating(monkeypatch):
    result, calls = _run_rm(monkeypatch, "--rating", "2", "--feedback", "slow disk")

    assert result.exit_code == 0, result.output
    assert calls == [{"feedback": "slow disk", "rating": 2}]


def test_rm_without_feedback_is_a_plain_remove(monkeypatch):
    result, calls = _run_rm(monkeypatch)

    assert result.exit_code == 0, result.output
    assert calls == [{}]


def test_rm_refuses_feedback_with_a_scheduled_removal(monkeypatch):
    result, calls = _run_rm(monkeypatch, "--in", "2h", "--rating", "3")

    assert result.exit_code != 0
    assert calls == []
