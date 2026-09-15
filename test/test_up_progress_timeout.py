"""DAH-2931 — `lium up` says what it is waiting on, is bounded by --timeout, and fails with the
actual state.

Observed: `lium up` sat more than five minutes with no output and no pod; an identical retry said
"no nodes available" at once. Nothing was printed between polls, there was no overall budget, a
rent the API refused or never answered surfaced as a generic error, and a pod that went
CREATION_FAILED was polled until its row vanished and then reported as "disappeared" without the
cause the backend had recorded. Builds on the PodStartError/--ready-timeout work (DAH-2558).
"""

from __future__ import annotations

from itertools import chain, repeat
from types import SimpleNamespace

import pytest
import requests
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.up import actions as up_actions
from lium.cli.up import command as up_command
from lium.cli.utils import EXIT_API_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import Config, Lium, LiumAuthError, LiumError, LiumNotFoundError, PodInfo, PodStartError

CREATE_FAILED_EVENT = {
    "event_type": "executor-rent.failed",
    "sub_event_type": "pod-create.failed",
    "error": "Container creation failed due to Failed create_container (failure_step: ssh_connect)",
}
LIFECYCLE_EVENT = {
    "event_type": "pod-lifecycle",
    "sub_event_type": "pod-lifecycle.status",
    "reason": "executor_offline",
    "detail": "node offline for 8 days",
}


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


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda *_: None)


# --- SDK: progress and cause ---------------------------------------------------------------------


def test_wait_ready_reports_every_poll_to_on_poll():
    client = _Client([[_pod("PENDING", None)], [_pod("RUNNING", None)], [_pod("RUNNING")]])
    seen = []

    pod = client.wait_ready("pod-1", timeout=None, on_poll=lambda p, status, elapsed: seen.append(status))

    assert pod.ssh_cmd
    assert seen == ["PENDING", "RUNNING", "RUNNING"]


def test_wait_ready_reports_missing_polls_too():
    client = _Client([[], [], [_pod("RUNNING")]])
    seen = []

    client.wait_ready("pod-1", timeout=None, on_poll=lambda p, status, elapsed: seen.append((p, status)))

    assert seen[:2] == [(None, "missing"), (None, "missing")]


def test_creation_failed_is_terminal_and_carries_the_recorded_cause():
    client = _Client([[_pod("PENDING", None)], [_pod("CREATION_FAILED", None)]], events=[CREATE_FAILED_EVENT])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("pod-1", timeout=None)

    assert failure.value.status == "CREATION_FAILED"
    assert failure.value.cause == CREATE_FAILED_EVENT["error"]
    assert "cause: Container creation failed due to Failed create_container (failure_step: ssh_connect)" in str(
        failure.value
    )
    assert client.event_requests == 1


@pytest.mark.parametrize("status", ["BROKEN", "REBOOT_FAILED", "DELETING"])
def test_the_backends_other_dead_end_statuses_are_terminal(status):
    client = _Client([[_pod(status, None)]])

    with pytest.raises(PodStartError):
        client.wait_ready("pod-1", timeout=None)


def test_a_vanished_pod_carries_its_lifecycle_reason():
    client = _Client([[_pod("PENDING", None)], []], events=[LIFECYCLE_EVENT])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("pod-1", timeout=None)

    assert failure.value.cause == "executor_offline: node offline for 8 days"


def test_pod_events_is_empty_against_a_backend_without_the_endpoint():
    client = _Client([[]], events_error=LiumNotFoundError("Resource not found"))

    assert client.pod_events("pod-1") == []
    assert client.pod_failure_cause("pod-1") is None


def test_pod_events_quotes_the_pod_id_and_asks_once():
    client = _Client([[]])
    seen = {}

    def _request(method, endpoint, **kwargs):
        seen["request"] = (method, endpoint, kwargs.get("retry"))
        return SimpleNamespace(json=lambda: [])

    client._request = _request

    assert client.pod_events("../admin?x=1") == []
    assert seen["request"] == ("GET", "/pods/..%2Fadmin%3Fx%3D1/events", False)


def test_pod_failure_cause_survives_an_api_error_on_the_failure_path():
    client = _Client([[]], events_error=LiumError("API error 500"))

    assert client.pod_failure_cause("pod-1") is None


def test_pod_failure_cause_survives_a_network_error_on_the_failure_path():
    # with_retry re-raises requests.RequestException (not a LiumError) after its last attempt
    client = _Client([[]], events_error=requests.ConnectionError("connection reset"))

    assert client.pod_failure_cause("pod-1") is None


def test_pod_failure_cause_prefers_the_latest_readable_event():
    client = _Client([[]], events=[LIFECYCLE_EVENT, {"sub_event_type": "pod-add-ssh-key.success"}, CREATE_FAILED_EVENT])

    assert client.pod_failure_cause("pod-1") == CREATE_FAILED_EVENT["error"]


def test_a_slow_pod_still_times_out_to_none_without_reading_events(monkeypatch):
    # three polls see PENDING before the 30 s budget ends: a slow pod is None, not an error, and
    # the events route is never read (that is the failure path's lookup)
    clock = chain([0, 0, 10, 20, 40], repeat(40))
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[_pod("PENDING", None)]], events_error=AssertionError("must not be called"))

    assert client.wait_ready("pod-1", timeout=30, poll_interval=10) is None
    assert client.event_requests == 0


# --- CLI: progress lines --------------------------------------------------------------------------


def test_progress_prints_on_status_change_and_every_thirty_seconds_only():
    lines = []
    on_poll = up_actions.WaitReadyAction()._progress(lines.append)
    pod = _pod("PENDING", None)

    for elapsed in (0, 10, 20, 30, 40):
        on_poll(pod, "PENDING", elapsed)
    on_poll(pod, "RUNNING", 50)
    on_poll(pod, "RUNNING", 60)

    assert lines == [
        "waiting for eager-wolf-aa… PENDING (0 s)",
        "waiting for eager-wolf-aa… PENDING (30 s)",
        "waiting for eager-wolf-aa… RUNNING (50 s)",
    ]


def test_progress_names_the_pod_generically_before_it_is_listed():
    lines = []
    on_poll = up_actions.WaitReadyAction()._progress(lines.append)

    on_poll(None, "missing", 0)

    assert lines == ["waiting for pod… missing (0 s)"]


def test_wait_ready_action_forwards_the_progress_callback():
    seen = {}

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval, on_poll=None):
            seen["on_poll"] = on_poll
            return _pod("RUNNING")

    result = up_actions.WaitReadyAction().execute({"lium": _Lium(), "pod_id": "pod-1", "report": lambda line: None})

    assert result.ok is True
    assert callable(seen["on_poll"])


def test_wait_ready_action_without_a_reporter_passes_no_callback():
    seen = {}

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval, on_poll=None):
            seen["on_poll"] = on_poll
            return _pod("RUNNING")

    up_actions.WaitReadyAction().execute({"lium": _Lium(), "pod_id": "pod-1"})

    assert seen["on_poll"] is None


# --- CLI: --timeout and the rent phase -------------------------------------------------------------


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


def test_wait_budget_is_what_the_deadline_leaves_capped_by_ready_timeout(monkeypatch):
    monkeypatch.setattr(up_command.time, "monotonic", lambda: 1000.0)

    assert up_command._wait_budget(1000.0 + 600, None) == 600
    assert up_command._wait_budget(1000.0 + 600, 90) == 90
    assert up_command._wait_budget(1000.0 + 30, 90) == 30
    # a deadline already passed still gives the wait one second, so the pod is polled once and named
    assert up_command._wait_budget(1000.0 - 5, None) == 1


def test_timeout_bounds_the_wait_and_ready_timeout_caps_it(monkeypatch):
    seen = {}

    class _Wait:
        def execute(self, ctx):
            seen["timeout"] = ctx["timeout"]
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    assert _run_up(monkeypatch, wait_action=_Wait, args=["--timeout", "600"]).exit_code == 0
    assert 0 < seen["timeout"] <= 600
    assert _run_up(monkeypatch, wait_action=_Wait, args=["--timeout", "600", "--ready-timeout", "90"]).exit_code == 0
    assert seen["timeout"] == 90


def test_wait_passes_a_reporter_so_progress_is_printed(monkeypatch):
    seen = {}

    class _Wait:
        def execute(self, ctx):
            seen["report"] = ctx.get("report")
            ctx["report"]("waiting for eager-wolf-aa… PENDING (30 s)")
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    result = _run_up(monkeypatch, wait_action=_Wait)

    assert result.exit_code == 0, result.output
    assert callable(seen["report"])
    assert "waiting for eager-wolf-aa… PENDING (30 s)" in _flat(result.output)
    assert "pod train (id: pod-1) created; waiting for it to become ready" in _flat(result.output)


def test_a_budget_that_runs_out_names_the_billing_pod(monkeypatch):
    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=False, data={}, error="still starting")

    result = _run_up(monkeypatch, wait_action=_Wait, args=["--timeout", "120"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Pod train (id: pod-1) is still starting after" in _flat(result.output)
    assert "(backend:" not in result.output
    assert "lium rm train" in _flat(result.output)


def test_a_budget_spent_before_the_rent_rents_no_pod(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(up_command.time, "monotonic", lambda: clock["now"])
    rented = []

    class _SlowResolve:
        def execute(self, ctx):
            clock["now"] += 121  # finding the node took longer than the whole --timeout
            return ActionResult(ok=True, data={"executor": _executor()})

    class _Rent:
        def execute(self, ctx):
            rented.append(ctx)
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    result = _run_up(monkeypatch, resolve_action=_SlowResolve, rent_action=_Rent, args=["--timeout", "120"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    output = _flat(result.output)
    assert "The --timeout budget of 120s ran out before renting brave-fox-3a; no pod was created." in output
    assert "volume" not in output
    assert rented == []


def test_a_budget_spent_creating_the_volume_names_the_volume_it_keeps(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(up_command.time, "monotonic", lambda: clock["now"])
    rented = []

    class _SlowVolume:
        def execute(self, ctx):
            clock["now"] += 121  # the volume request retried past the whole --timeout
            return ActionResult(ok=True, data={"volume": None, "volume_id": "vol-1"})

    class _Rent:
        def execute(self, ctx):
            rented.append(ctx)
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    monkeypatch.setattr(up_command, "CreateVolumeAction", _SlowVolume)
    result = _run_up(monkeypatch, rent_action=_Rent, args=["--timeout", "120", "--volume", "new:name=my-data"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    output = _flat(result.output)
    assert "ran out before renting brave-fox-3a; no pod was created. The volume my-data was created and is kept." in output
    assert rented == []


def test_a_budget_with_time_left_reaches_the_rent(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(up_command.time, "monotonic", lambda: clock["now"])
    rented = []

    class _Resolve:
        def execute(self, ctx):
            clock["now"] += 30
            return ActionResult(ok=True, data={"executor": _executor()})

    class _Rent:
        def execute(self, ctx):
            rented.append(ctx)
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    seen = {}

    class _Wait:
        def execute(self, ctx):
            seen["timeout"] = ctx["timeout"]
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    result = _run_up(monkeypatch, resolve_action=_Resolve, rent_action=_Rent, wait_action=_Wait, args=["--timeout", "120"])

    assert result.exit_code == 0, result.output
    assert len(rented) == 1
    assert seen["timeout"] == 90  # the 30 s spent finding the node came off the wait's budget


def test_an_unanswered_rent_request_says_to_check_ps_before_retrying(monkeypatch):
    class _Rent:
        def execute(self, ctx):
            raise requests.exceptions.ConnectTimeout("connect timed out")

    result = _run_up(monkeypatch, rent_action=_Rent)

    assert result.exit_code == EXIT_API_ERROR
    output = _flat(result.output)
    assert "The rent request for brave-fox-3a got no answer from the API (ConnectTimeout)" in output
    assert "Run 'lium ps' before retrying" in output
    assert "may exist and be billing" in output


def test_a_rejected_rent_names_the_node_and_the_reason_and_points_at_ps(monkeypatch):
    # Lium.up() retries an unanswered rent once; a refusal of that retry can mean the first
    # request did create a pod, so the message must not promise that none exists.
    class _Rent:
        def execute(self, ctx):
            raise LiumError("API error 400: Executor has a pending rental")

    result = _run_up(monkeypatch, rent_action=_Rent)

    assert result.exit_code == EXIT_API_ERROR
    output = _flat(result.output)
    assert "Node brave-fox-3a could not be rented: API error 400: Executor has a pending rental" in output
    assert "Run 'lium ps' to check whether a pod was created" in output
    assert "No pod was created" not in output
    assert "lium ls --format json" in output


def test_an_auth_failure_on_rent_is_not_reported_as_a_rejected_rent(monkeypatch):
    class _Rent:
        def execute(self, ctx):
            raise LiumAuthError("Invalid API key")

    result = _run_up(monkeypatch, rent_action=_Rent)

    assert result.exit_code != 0
    assert "could not be rented" not in result.output
    assert "Invalid API key" in result.output


def test_a_failed_start_shows_the_cause_in_the_message(monkeypatch):
    class _Wait:
        def execute(self, ctx):
            raise PodStartError(
                "Pod eager-wolf-aa (pod-1) will not start: status CREATION_FAILED (seen: PENDING → CREATION_FAILED); "
                "cause: Container creation failed due to Failed create_container (failure_step: ssh_connect)",
                pod_id="pod-1", pod=_pod("CREATION_FAILED", None), status="CREATION_FAILED",
                cause="Container creation failed due to Failed create_container (failure_step: ssh_connect)",
            )

    result = _run_up(monkeypatch, wait_action=_Wait)

    assert result.exit_code == EXIT_API_ERROR
    assert "failure_step: ssh_connect" in _flat(result.output)


def test_help_documents_timeout_with_its_default():
    result = CliRunner().invoke(up_command.up_command, ["--help"])

    assert "--timeout" in result.output
    assert "900" in result.output
