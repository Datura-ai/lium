"""`lium reset-gpu` / `Lium.reset_gpu` (DAH-4658)."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.reset_gpu import command as reset_gpu_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import Config, GpuResetOutcome, GpuResetResult, Lium, LiumError, LiumGpuOperationError

OK = {
    "pod_id": "pod-uuid-1", "ok": True, "container_restarted": True,
    "gpus": [{"index": 0, "uuid": "GPU-aaa", "result": "reset", "detail": None}],
}
RESULT = GpuResetResult(pod_id="pod-uuid-1", ok=True, container_restarted=True,
                        gpus=[GpuResetOutcome(index=0, uuid="GPU-aaa", result="reset")])
FAILED_GPU = {"index": 0, "uuid": "GPU-aaa", "result": "failed", "detail": "refused"}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers, self.text = status, body, {}, json.dumps(body)
        self.ok = status < 400

    def json(self):
        return self._body


def test_sdk_posts_indices_once_with_a_long_timeout(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        return _Resp(200, OK)

    monkeypatch.setattr("lium.sdk.client.requests.request", fake)
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "_release_ssh", lambda pod: None)
    pod = SimpleNamespace(id="pod-uuid-1")

    assert client.reset_gpu(pod, [0, 2]) == RESULT
    assert client.reset_gpu(pod) == RESULT
    (m1, u1, kw1), (_, _, kw2) = calls
    assert m1 == "POST" and u1.endswith("/pods/pod-uuid-1/reset-gpu")
    assert kw1["json"] == {"gpu_indices": [0, 2]} and kw2["json"] == {"gpu_indices": None}
    assert kw1["timeout"] >= 90


def test_sdk_409_carries_the_api_code(monkeypatch):
    body = {"detail": {"code": "mig_enabled", "message": "MIG is on"},
            "error": {"code": "mig_enabled", "message": "MIG is on"}}
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: _Resp(409, body))
    with pytest.raises(LiumError) as exc:
        Lium(Config(api_key="test")).reset_gpu(SimpleNamespace(id="p"))
    assert exc.value.code == "mig_enabled" and not isinstance(exc.value, LiumGpuOperationError)


def test_sdk_failed_gpu_409_keeps_the_gpus_and_releases_ssh(monkeypatch):
    detail = {"code": "reset_not_supported", "message": "GPU 0 failed", "container_restarted": True, "gpus": [FAILED_GPU]}
    body = {"message": detail, "error": {"code": "reset_not_supported", "message": "GPU 0 failed"}}
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: _Resp(409, body))
    client = Lium(Config(api_key="test"))
    released = []
    monkeypatch.setattr(client, "_release_ssh", released.append)
    pod = SimpleNamespace(id="p")
    with pytest.raises(LiumGpuOperationError) as exc:
        client.reset_gpu(pod)
    assert exc.value.code == "reset_not_supported" and exc.value.container_restarted is True
    assert exc.value.gpus == [GpuResetOutcome(**FAILED_GPU)]
    assert released == [pod]


def _run(monkeypatch, args, outcome=RESULT):
    seen = {}

    class _Lium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name="my-pod", executor=None, created_at="")]

        def reset_gpu(self, pod, gpu_indices=None):
            seen["gpu_indices"] = gpu_indices
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(reset_gpu_module, "Lium", _Lium)
    return CliRunner().invoke(cli, ["reset-gpu", *args]), seen


def test_gpu_flags_become_indices(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod", "--gpu", "0", "--gpu", "2", "--yes"])
    assert result.exit_code == 0, result.output
    assert seen["gpu_indices"] == [0, 2]
    assert "GPU-aaa" in result.output


def test_no_gpu_flag_resets_all_and_json_is_the_response(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod", "--yes", "--json"])
    assert result.exit_code == 0, result.output
    assert seen["gpu_indices"] is None
    assert json.loads(result.stdout) == OK


def test_negative_gpu_index_is_refused(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod", "--gpu", "-1", "--yes"])
    assert result.exit_code == 2 and not seen


def test_without_a_terminal_it_asks_for_yes(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod"])
    assert result.exit_code == EXIT_CONFIGURATION_ERROR and not seen


def test_409_exits_non_zero_with_the_code(monkeypatch):
    result, _ = _run(monkeypatch, ["my-pod", "--yes", "--json"],
                     LiumError("API error 409: MIG is on", code="mig_enabled"))
    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "mig_enabled"


def test_a_failed_gpu_shows_the_gpus_and_exits_non_zero(monkeypatch):
    failed = LiumGpuOperationError("API error 409: GPU 0 failed", code="reset_not_supported",
                                   container_restarted=True, gpus=[GpuResetOutcome(**FAILED_GPU)])
    result, _ = _run(monkeypatch, ["my-pod", "--yes"], failed)
    assert result.exit_code == EXIT_API_ERROR
    assert "refused" in result.output

    result, _ = _run(monkeypatch, ["my-pod", "--yes", "--json"], failed)
    envelope = json.loads(result.stderr)
    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "reset_not_supported"
    assert envelope["data"]["gpus"] == [FAILED_GPU] and envelope["data"]["container_restarted"] is True
