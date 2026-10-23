"""`lium reset-gpu` / `Lium.reset_gpu` (DAH-4658)."""

import json
from types import SimpleNamespace

import pytest
import requests
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.reset_gpu import command as reset_gpu_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import Config, GpuResetOutcome, GpuResetResult, Lium, LiumError, LiumGpuOperationError, LiumServerError

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
    body = {"detail": {"code": "mig_enabled", "message": "MIG is on", "gpus": []},
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

        def reset_gpu(self, pod, gpu_indices=None, on_progress=None):
            seen["gpu_indices"] = gpu_indices
            on_progress(3.0)  # the command always shows progress while the reset runs
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
    assert "refused" in result.output and "still stopped" not in result.output

    failed.container_restarted = False
    result, _ = _run(monkeypatch, ["my-pod", "--yes"], failed)
    assert "still stopped" in result.output
    failed.container_restarted = True

    result, _ = _run(monkeypatch, ["my-pod", "--yes", "--json"], failed)
    envelope = json.loads(result.stderr)
    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "reset_not_supported"
    assert envelope["data"]["gpus"] == [FAILED_GPU] and envelope["data"]["container_restarted"] is True


# -- async API: POST answers 202 with a request_id, GET polls it

API = "https://lium.io/api"
POD_ID = "pod-uuid-1"
POST_URL = f"{API}/pods/{POD_ID}/reset-gpu"
GET_URL = f"{POST_URL}/req-1"
RUNNING = {"pod_id": POD_ID, "request_id": "req-1", "status": "running", "ok": None,
           "container_restarted": None, "gpus": [], "error": None}
DONE = {**RUNNING, "status": "done", "ok": True, "container_restarted": True, "gpus": OK["gpus"]}


@pytest.fixture
def clock(monkeypatch):
    """A fake clock: sleep moves time.monotonic forward, so a deadline test does not wait."""
    now = [1000.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("lium.sdk.client.time.sleep", sleep)
    monkeypatch.setattr("lium.sdk.client.time.monotonic", lambda: now[0])
    return sleeps


def _client(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "_release_ssh", lambda pod: None)
    return client


@responses.activate
def test_sdk_polls_a_running_reset_until_done(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=RUNNING)
    responses.get(GET_URL, json=DONE)
    progress = []

    result = _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID), [0], on_progress=progress.append)

    assert result == RESULT
    assert [c.request.method for c in responses.calls] == ["POST", "GET", "GET"]
    assert clock == [3, 3] and len(progress) == 2


@responses.activate
def test_sdk_failed_status_raises_the_backend_code(monkeypatch, clock):
    failed = {**RUNNING, "status": "failed", "ok": False, "container_restarted": True, "gpus": [FAILED_GPU],
              "error": {"code": "gpu_in_use_outside_pod", "message": "GPU 0 is used outside the pod"}}
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=failed)

    with pytest.raises(LiumGpuOperationError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_in_use_outside_pod" and exc.value.container_restarted is True
    assert exc.value.gpus == [GpuResetOutcome(**FAILED_GPU)]


@responses.activate
def test_sdk_failed_status_without_gpus_is_a_plain_error(monkeypatch, clock):
    failed = {**RUNNING, "status": "failed", "ok": False, "error": {"code": "mig_enabled", "message": "MIG is on"}}
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=failed)

    with pytest.raises(LiumError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "mig_enabled" and not isinstance(exc.value, LiumGpuOperationError)


@responses.activate
def test_sdk_server_timeout_status_says_the_reset_may_still_finish(monkeypatch, clock):
    timeout = {**RUNNING, "status": "timeout",
               "error": {"code": "gpu_reset_unavailable", "message": "The validator did not answer in time"}}
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=timeout)

    with pytest.raises(LiumServerError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_reset_unavailable" and "still" in exc.value.hint


@responses.activate
def test_sdk_client_deadline_stops_polling_with_a_timeout_error(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=RUNNING)

    with pytest.raises(LiumServerError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_reset_timeout" and "req-1" in str(exc.value)
    assert sum(clock) > 300  # past the server's own 300 s deadline


@responses.activate
def test_sdk_poll_failure_says_the_reset_may_still_run(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, status=503, json={"detail": "unavailable"})

    with pytest.raises(LiumServerError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_reset_timeout" and "still" in exc.value.hint


@responses.activate
def test_sdk_legacy_200_is_the_result_without_polling(monkeypatch, clock):
    responses.post(POST_URL, json=OK)

    assert _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID)) == RESULT
    assert len(responses.calls) == 1 and clock == []


def test_cli_progress_stays_off_stdout_under_json(monkeypatch):
    result, _ = _run(monkeypatch, ["my-pod", "--yes", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == OK


def test_cli_client_timeout_says_the_reset_may_still_run(monkeypatch):
    timeout = LiumServerError("GPU reset req-1 did not finish in 330 s", code="gpu_reset_timeout")
    result, _ = _run(monkeypatch, ["my-pod", "--yes", "--json"], timeout)

    envelope = json.loads(result.stderr.strip().splitlines()[-1])
    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "gpu_reset_timeout"
    hint = envelope["error"]["hint"]
    assert "still" in hint and "eager-wolf-aa" in hint and "Retry" not in hint


@responses.activate
def test_sdk_keeps_polling_through_5xx_and_network_errors(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, status=503, json={"detail": "unavailable"})
    responses.get(GET_URL, body=requests.ConnectionError("connection reset"))
    responses.get(GET_URL, json=DONE)

    result = _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert result == RESULT
    assert [c.request.method for c in responses.calls] == ["POST", "GET", "GET", "GET"]
    assert clock == [3, 3, 3]


@responses.activate
def test_sdk_poll_timeout_is_clamped_to_the_time_left(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, json=RUNNING)

    with pytest.raises(LiumServerError):
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    timeouts = [c.request.req_kwargs["timeout"] for c in responses.calls[1:]]
    assert max(timeouts) <= 30 and timeouts[-1] <= 3


@responses.activate
def test_sdk_network_error_on_post_says_the_reset_may_still_run(monkeypatch, clock):
    responses.post(POST_URL, body=requests.ConnectionError("connection reset"))

    with pytest.raises(LiumServerError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_reset_timeout" and "still" in exc.value.hint
    assert len(responses.calls) == 1


@responses.activate
def test_sdk_read_timeouts_on_every_poll_end_in_a_timeout_error(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, body=requests.ReadTimeout("read timed out"))

    with pytest.raises(LiumServerError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "gpu_reset_timeout" and "still" in exc.value.hint


def _run_real_sdk(monkeypatch):
    pod = SimpleNamespace(id=POD_ID, huid="eager-wolf-aa", name="my-pod", executor=None, created_at="")

    class _Lium(Lium):
        def __init__(self, *a, **kw):
            super().__init__(Config(api_key="test"))

        def ps(self):
            return [pod]

        def _release_ssh(self, pod):
            pass

    monkeypatch.setattr(reset_gpu_module, "Lium", _Lium)
    result = CliRunner().invoke(cli, ["reset-gpu", "my-pod", "--yes", "--json"])
    return result, json.loads(result.stderr.strip().splitlines()[-1])


@responses.activate
def test_cli_network_error_on_post_says_the_reset_may_still_run(monkeypatch, clock):
    responses.post(POST_URL, body=requests.ConnectionError("connection reset"))

    result, envelope = _run_real_sdk(monkeypatch)

    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "gpu_reset_timeout"
    assert "still" in envelope["error"]["hint"] and "eager-wolf-aa" in envelope["error"]["hint"]


@responses.activate
def test_cli_read_timeout_on_polls_says_the_reset_may_still_run(monkeypatch, clock):
    responses.post(POST_URL, status=202, json=RUNNING)
    responses.get(GET_URL, body=requests.ReadTimeout("read timed out"))

    result, envelope = _run_real_sdk(monkeypatch)

    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "gpu_reset_timeout"
    assert "still" in envelope["error"]["hint"] and "eager-wolf-aa" in envelope["error"]["hint"]


@responses.activate
def test_sdk_legacy_200_with_ok_false_raises_like_a_failed_status(monkeypatch, clock):
    legacy = {**OK, "ok": False, "gpus": [FAILED_GPU]}
    responses.post(POST_URL, json=legacy)

    with pytest.raises(LiumGpuOperationError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "reset_not_supported" and exc.value.gpus == [GpuResetOutcome(**FAILED_GPU)]


@responses.activate
def test_sdk_legacy_200_with_ok_false_and_no_gpus_is_a_plain_error(monkeypatch, clock):
    responses.post(POST_URL, json={**OK, "ok": False, "gpus": []})

    with pytest.raises(LiumError) as exc:
        _client(monkeypatch).reset_gpu(SimpleNamespace(id=POD_ID))

    assert exc.value.code == "reset_not_supported" and not isinstance(exc.value, LiumGpuOperationError)


@responses.activate
def test_cli_legacy_200_with_ok_false_fails_like_a_failed_status(monkeypatch, clock):
    responses.post(POST_URL, json={**OK, "ok": False, "gpus": [FAILED_GPU]})

    result, envelope = _run_real_sdk(monkeypatch)

    assert result.exit_code == EXIT_API_ERROR and envelope["error"]["code"] == "reset_not_supported"
    assert envelope["data"]["gpus"] == [FAILED_GPU]
