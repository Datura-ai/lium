"""`lium gpu-clocks` / `Lium.lock_gpu_clocks` / `Lium.reset_gpu_clocks` (DAH-4663)."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.gpu_clocks import command as gpu_clocks_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR, EXIT_POD_NOT_FOUND
from lium.sdk import Config, Lium, LiumError

OK = {
    "pod_id": "pod-uuid-1", "ok": True,
    "gpus": [{"index": 0, "uuid": "GPU-aaa", "result": "locked",
              "applied_min_mhz": 1400, "applied_max_mhz": 1800, "detail": None}],
}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body, self.headers, self.text = status, body, {}, json.dumps(body)
        self.ok = status < 400

    def json(self):
        return self._body


def test_sdk_lock_and_reset_post_once_with_a_long_timeout(monkeypatch):
    calls = []

    def fake(method, url, **kw):
        calls.append((method, url, kw))
        return _Resp(200, OK)

    monkeypatch.setattr("lium.sdk.client.requests.request", fake)
    client = Lium(Config(api_key="test"))
    pod = SimpleNamespace(id="pod-uuid-1")

    assert client.lock_gpu_clocks(pod, 1400, 1800, [0, 2]) == OK
    assert client.reset_gpu_clocks(pod) == OK
    assert len(calls) == 2
    (m1, u1, kw1), (_, u2, kw2) = calls
    assert m1 == "POST" and u1.endswith("/pods/pod-uuid-1/gpu-clocks") and u2 == u1
    assert kw1["json"] == {"gpu_indices": [0, 2], "min_mhz": 1400, "max_mhz": 1800}
    assert kw2["json"] == {"gpu_indices": None, "reset": True}
    assert kw1["timeout"] >= 90


@pytest.mark.parametrize("status,code", [(409, "clock_lock_not_supported"), (503, "gpu_clocks_unavailable")])
def test_sdk_refusal_carries_the_api_code(monkeypatch, status, code):
    body = {"detail": {"code": code, "message": "no"}, "error": {"code": code, "message": "no"}}
    calls = []
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: calls.append(1) or _Resp(status, body))
    with pytest.raises(LiumError) as exc:
        Lium(Config(api_key="test")).lock_gpu_clocks(SimpleNamespace(id="p"), 1400, 1800)
    assert exc.value.code == code and len(calls) == 1


def _run(monkeypatch, args, outcome=OK):
    seen = {}

    class _Lium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return [SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name="my-pod", executor=None, created_at="")]

        def lock_gpu_clocks(self, pod, min_mhz, max_mhz, gpu_indices=None):
            seen.update(lock=(min_mhz, max_mhz), gpu_indices=gpu_indices)
            return self._out()

        def reset_gpu_clocks(self, pod, gpu_indices=None):
            seen.update(reset=True, gpu_indices=gpu_indices)
            return self._out()

        def _out(self):
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr(gpu_clocks_module, "Lium", _Lium)
    return CliRunner().invoke(cli, ["gpu-clocks", *args]), seen


def test_lock_prints_the_applied_clocks_without_asking(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod", "--lock", "1400,1800", "--gpu", "0", "--gpu", "2"])
    assert result.exit_code == 0, result.output
    assert seen == {"lock": (1400, 1800), "gpu_indices": [0, 2]}
    assert "1400" in result.output and "1800" in result.output and "GPU-aaa" in result.output


def test_reset_all_and_json_is_the_response(monkeypatch):
    result, seen = _run(monkeypatch, ["my-pod", "--reset", "--json"])
    assert result.exit_code == 0, result.output
    assert seen == {"reset": True, "gpu_indices": None}
    assert json.loads(result.stdout) == OK


@pytest.mark.parametrize("args", [["my-pod"], ["my-pod", "--reset", "--lock", "1,2"]])
def test_exactly_one_of_lock_or_reset(monkeypatch, args):
    result, seen = _run(monkeypatch, args)
    assert result.exit_code == EXIT_CONFIGURATION_ERROR and not seen


@pytest.mark.parametrize("value", ["1800", "1800,1400", "a,b", "0,100"])
def test_bad_lock_range_is_a_usage_error(monkeypatch, value):
    result, seen = _run(monkeypatch, ["my-pod", "--lock", value])
    assert result.exit_code == 2 and not seen


def test_missing_pod_fails_before_any_call(monkeypatch):
    result, seen = _run(monkeypatch, ["no-such-pod-zz", "--reset"])
    assert result.exit_code == EXIT_POD_NOT_FOUND and not seen


def test_409_exits_3_with_the_code(monkeypatch):
    result, _ = _run(monkeypatch, ["my-pod", "--lock", "1400,1800", "--json"],
                     LiumError("API error 409: MIG is on", code="mig_enabled"))
    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "mig_enabled"


def test_a_failed_gpu_exits_1(monkeypatch):
    failed = {**OK, "ok": False, "gpus": [{"index": 0, "uuid": "GPU-aaa", "result": "failed",
                                           "applied_min_mhz": None, "applied_max_mhz": None, "detail": "refused"}]}
    result, _ = _run(monkeypatch, ["my-pod", "--reset"], failed)
    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "refused" in result.output
