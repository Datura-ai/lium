"""DAH-4993: a 429 during `lium up`'s readiness wait slows the wait down instead of ending it.

Five parallel `lium up` from one IP (dogfood 22 Oct 2026) got 429 on the wait's `GET /pods` past the
SDK's three retries and exited `rate_limited` with the pod billing, no Jupyter and no GPU check.
"""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.up import actions as up_actions
from lium.cli.up import command as up_command
from lium.cli.utils import EXIT_GENERAL_ERROR
from lium.sdk import (
    Config,
    LiumRateLimitError,
    PodInfo,
)
from lium.sdk.client import Lium


# helpers from test/test_up_progress_timeout.py, inlined when that file was cut
def _pod(status: str, ssh_cmd: str | None = "ssh user@pod.example -p 20299") -> PodInfo:
    return PodInfo(
        id="pod-1",
        name="train",
        status=status,
        huid="eager-wolf-aa",
        ssh_cmd=ssh_cmd,
        ports={},
        created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )

class _Client(Lium):
    """A client whose `ps` replays a scripted sequence of pod lists and whose event log is fixed."""

    def __init__(self, sequence, events=None, events_error=None):
        super().__init__(Config(api_key="test"))
        self.sequence = list(sequence)
        self.events = events or []
        self.events_error = events_error
        self.event_requests = 0

    def ps(self):
        if not self.sequence:
            return []
        return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]

    def _request(self, method, endpoint, **kwargs):
        assert (method, endpoint) == ("GET", "/pods/pod-1/events")
        self.event_requests += 1
        if self.events_error:
            raise self.events_error
        return SimpleNamespace(json=lambda: self.events)

def _executor():
    return SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

def _run_up(monkeypatch, *, resolve_action=None, rent_action=None, wait_action=None, args=()):
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def get_deployment_estimate(self, *a, **k):
            return {}

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": _executor()})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", resolve_action or _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", rent_action or _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", wait_action or _Wait)
    return CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--no-ssh", *args])

def _flat(output: str) -> str:
    return " ".join(output.split())



class _Throttled(_Client):
    """`ps` raises 429 ``throttled`` times (forever with None), then replays the sequence."""

    def __init__(self, sequence, throttled, retry_after=None):
        super().__init__(sequence)
        self.throttled = throttled
        self.retry_after = retry_after

    def ps(self):
        if self.throttled is None or self.throttled > 0:
            if self.throttled:
                self.throttled -= 1
            exc = LiumRateLimitError("Rate limit exceeded")
            exc.retry_after = self.retry_after
            raise exc
        return super().ps()


def _wait_with(client):
    class _Wait(up_actions.WaitReadyAction):
        def execute(self, ctx):
            return super().execute({**ctx, "lium": client})
    return _Wait


def test_two_429s_during_the_wait_slow_it_down_and_up_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr("lium.sdk.client.time.sleep", sleeps.append)
    client = _Throttled([[_pod("RUNNING")]], throttled=2, retry_after=7)

    result = _run_up(monkeypatch, wait_action=_wait_with(client), args=["--timeout", "600"])

    assert result.exit_code == 0, result.output
    assert sleeps == [7, 7]  # Retry-After, not the 1 s poll


def test_without_retry_after_the_interval_grows(monkeypatch):
    sleeps = []
    monkeypatch.setattr("lium.sdk.client.time.sleep", sleeps.append)
    client = _Throttled([[_pod("RUNNING")]], throttled=3)

    assert client.wait_ready("pod-1", timeout=600).status == "RUNNING"
    assert sleeps == [2, 4, 8]  # the 1 s early poll, doubled per 429


def test_a_429_that_outlasts_the_budget_still_names_the_billing_pod(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: clock["now"])
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda s: clock.__setitem__("now", clock["now"] + s))
    client = _Throttled([[_pod("RUNNING")]], throttled=None, retry_after=30)

    result = _run_up(monkeypatch, wait_action=_wait_with(client), args=["--timeout", "120"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Pod train (id: pod-1) is still starting after" in _flat(result.output)
    assert "lium rm train" in _flat(result.output)


def test_retry_after_is_read_from_the_429_response():
    resp = SimpleNamespace(ok=False, status_code=429, headers={"Retry-After": "12"}, json=lambda: {}, text="")
    try:
        Lium._raise_for_status(resp)
    except LiumRateLimitError as exc:
        assert exc.retry_after == 12.0
    else:
        raise AssertionError("no LiumRateLimitError")


class _MissingThenThrottled(_Client):
    """The first `ps` lists no pod (inside the listing grace), every later one is a 429."""

    def ps(self):
        if not getattr(self, "listed_once", False):
            self.listed_once = True
            return []
        raise LiumRateLimitError("Rate limit exceeded")


def test_a_429_after_one_empty_listing_is_still_starting_not_failed(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: clock["now"])
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda s: clock.__setitem__("now", clock["now"] + s))

    assert _MissingThenThrottled([]).wait_ready("pod-1", timeout=20) is None  # not PodStartError


def test_the_jupyter_wait_stops_at_its_limit_counting_time_inside_ps(monkeypatch):
    clock = {"now": 0.0}
    calls = []
    monkeypatch.setattr(up_actions.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(up_actions.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))

    def ps():
        calls.append(clock["now"])
        clock["now"] += 10  # the SDK's own retries and the request take time too
        raise LiumRateLimitError("Rate limit exceeded")

    lium = SimpleNamespace(install_jupyter=lambda *a, **k: None, ps=ps)
    pod = SimpleNamespace(id="pod-1", ports={"22": 1, "8888": 2})
    result = up_actions.InstallJupyterAction().execute({"lium": lium, "pod": pod})

    assert not result.ok and "timed out" in result.error
    assert clock["now"] <= 120 + 10 and max(calls) < 120


def test_the_jupyter_wait_honours_retry_after(monkeypatch):
    clock = {"now": 0.0}
    calls = []
    monkeypatch.setattr(up_actions.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(up_actions.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))

    def ps():
        calls.append(clock["now"])
        exc = LiumRateLimitError("Rate limit exceeded")
        exc.retry_after = 60
        raise exc

    lium = SimpleNamespace(install_jupyter=lambda *a, **k: None, ps=ps)
    up_actions.InstallJupyterAction().execute({"lium": lium, "pod": SimpleNamespace(id="pod-1", ports={"8888": 2})})

    assert calls == [3, 63]  # the next poll waits the 60 s the server asked for


def test_the_jupyter_wait_returns_to_the_normal_poll_once_the_api_answers(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(up_actions.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(up_actions.time, "sleep", lambda s: clock.__setitem__("now", clock["now"] + s))

    def ps():
        if clock["now"] < 10:
            exc = LiumRateLimitError("Rate limit exceeded")
            exc.retry_after = 60
            raise exc
        status = "SUCCESS" if clock["now"] >= 70 else "INSTALLING"
        return [SimpleNamespace(id="pod-1", huid="x", name="x", jupyter_installation_status=status, jupyter_url="u")]

    lium = SimpleNamespace(install_jupyter=lambda *a, **k: None, ps=ps)
    result = up_actions.InstallJupyterAction().execute({"lium": lium, "pod": SimpleNamespace(id="pod-1", ports={"8888": 2})})

    assert result.ok and clock["now"] < 80  # 429 at 3 s, INSTALLING at 63 s, SUCCESS seen soon after 70 s
