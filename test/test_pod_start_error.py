"""DAH-2558: a pod that will never start is an error, not a timeout.

`Lium.wait_ready()` used to poll only for RUNNING + ssh and returned None for
everything else — a FAILED pod, a pod removed under us, a wrong id — after
burning the whole timeout. The caller could not tell a slow start from a dead
pod, so it either retried (and paid for another pod) or gave up on a pod that
was fine. `lium up` had no bound at all and hung on a PENDING node.
"""

from itertools import chain
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.up import actions as up_actions
from lium.cli.up import command as up_command
from lium.cli.utils import EXIT_API_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import Config, Lium, PodInfo, PodStartError


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
    """A client whose `ps` replays a scripted sequence of pod lists and whose event log is empty.

    `pod_events` is answered here so the failure path (`pod_failure_cause`) never opens a
    connection: a unit test must not reach the API.
    """

    def __init__(self, sequence):
        super().__init__(Config(api_key="test"))
        self.sequence = list(sequence)
        self.calls = 0
        self.event_requests = 0

    def pod_events(self, pod_id):
        self.event_requests += 1
        return []

    def ps(self):
        self.calls += 1
        if not self.sequence:
            return []
        return self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda *_: None)


# --- SDK ------------------------------------------------------------------------------------


def test_wait_ready_returns_the_pod_once_running_with_ssh():
    client = _Client([[_pod("PENDING", None)], [_pod("RUNNING", None)], [_pod("RUNNING")]])

    ready = client.wait_ready("pod-1", timeout=60, poll_interval=1)

    assert ready.status == "RUNNING" and ready.ssh_cmd
    assert client.calls == 3


def test_wait_ready_raises_on_a_terminal_status_with_the_history():
    client = _Client([[_pod("PENDING", None)], [_pod("FAILED", None)]])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("pod-1", timeout=600, poll_interval=1)

    err = failure.value
    assert err.pod_id == "pod-1"
    assert err.status == "FAILED"
    assert err.history == ["PENDING", "FAILED"]
    assert err.pod.huid == "eager-wolf-aa"
    assert "FAILED" in str(err) and "PENDING → FAILED" in str(err)
    assert client.calls == 2, "a dead pod must not be polled until the timeout"


@pytest.mark.parametrize("status", ["STOPPED", "failed", "TERMINATED"])
def test_wait_ready_treats_every_terminal_status_alike(status):
    client = _Client([[_pod(status, None)]])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready(_pod("PENDING", None), timeout=600, poll_interval=1)

    assert failure.value.status == status.upper()


def test_wait_ready_raises_when_a_seen_pod_disappears():
    client = _Client([[_pod("PENDING", None)], []])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready({"id": "pod-1"}, timeout=600, poll_interval=1)

    assert "disappeared" in str(failure.value)
    assert failure.value.pod.huid == "eager-wolf-aa"
    assert failure.value.history == ["PENDING"]
    assert client.event_requests == 1  # the cause was looked up once, from the double, not the API


def test_a_vanished_pods_cause_is_in_the_message_not_only_on_the_error():
    class _WithCause(_Client):
        def pod_events(self, pod_id):
            self.event_requests += 1
            return [{"error": "executor reclaimed the GPU"}]

    client = _WithCause([[_pod("PENDING", None)], []])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready({"id": "pod-1"}, timeout=600, poll_interval=1)

    assert str(failure.value).endswith("; cause: executor reclaimed the GPU")
    assert failure.value.cause == "executor reclaimed the GPU"


def test_wait_ready_raises_for_a_pod_that_is_never_listed(monkeypatch):
    """A wrong id is reported after 20 s of empty listings, with a long budget still open."""
    clock = iter([0, 0, 10, 20])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[]])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("00000000-0000-0000-0000-000000000000", timeout=600, poll_interval=10)

    assert client.calls == 3
    assert "after 3 checks over 20 s" in str(failure.value)
    assert failure.value.pod is None and failure.value.status is None
    assert client.event_requests == 1


def test_wait_ready_raises_for_a_never_listed_pod_when_the_budget_ends_with_the_grace(monkeypatch):
    """The audit case: wait_ready('00000000-…', timeout=20) burned 21.5 s and returned None.

    The timeout check runs before the grace check; when both are 20 s the wrong id must still
    be an error, not a "still starting" None."""
    clock = iter([0, 0, 10, 20])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[]])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("00000000-0000-0000-0000-000000000000", timeout=20, poll_interval=10)

    assert client.calls == 2
    assert "after 2 checks over 20 s" in str(failure.value)
    assert failure.value.pod is None


def test_wait_ready_returns_none_for_a_never_listed_pod_inside_a_short_budget(monkeypatch):
    """A wait that ends before 20 s have passed knows nothing: a timeout, as before (the rule keys
    on the seconds elapsed when the loop wakes, not on the budget asked for)."""
    clock = iter([0, 0, 5, 10])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[]])

    assert client.wait_ready("pod-1", timeout=10, poll_interval=5) is None
    assert client.event_requests == 0


def test_wait_ready_gives_a_never_listed_pod_twenty_seconds_not_three_polls(monkeypatch):
    """At the 2 s schedule a poll count of 3 would fail the wait 4 s in; the grace is a time budget."""
    clock = chain([0], range(0, 21, 2))
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[]])

    with pytest.raises(PodStartError) as failure:
        client.wait_ready("pod-1", timeout=600)

    assert client.calls == 11
    assert "after 11 checks over 20 s" in str(failure.value)


def test_wait_ready_survives_a_listing_hiccup_on_a_pod_not_yet_seen(monkeypatch):
    """Three empty listings in the first 6 s are a hiccup, not a missing pod (arhangel66, lium#172)."""
    clock = iter([0, 0, 2, 4, 6])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[], [], [], [_pod("RUNNING")]])

    assert client.wait_ready("pod-1", timeout=600).status == "RUNNING"
    assert client.calls == 4


def test_wait_ready_still_returns_none_when_a_slow_pod_outlives_the_timeout(monkeypatch):
    clock = iter([0, 0, 100, 200, 400])
    monkeypatch.setattr("lium.sdk.client.time.time", lambda: next(clock))
    client = _Client([[_pod("PENDING", None)]])

    assert client.wait_ready("pod-1", timeout=300, poll_interval=1) is None


def test_wait_ready_with_timeout_none_waits_until_ready():
    client = _Client([[_pod("PENDING", None)]] * 5 + [[_pod("RUNNING")]])

    assert client.wait_ready("pod-1", timeout=None, poll_interval=1).status == "RUNNING"
    assert client.calls == 6


def test_pod_start_error_is_a_lium_error():
    from lium.sdk import LiumError

    assert issubclass(PodStartError, LiumError)


# --- CLI ------------------------------------------------------------------------------------


def _run_up(monkeypatch, wait_action, args=()):
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def get_deployment_estimate(self, *a, **k):
            return {}

        def schedule_termination(self, pod, *, termination_time):
            return {"removal_scheduled_at": termination_time}

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", wait_action)
    return CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--no-ssh", *args])


def test_up_exits_non_zero_with_the_last_status_when_the_pod_fails(monkeypatch):
    class _Wait:
        def execute(self, ctx):
            raise PodStartError(
                "Pod eager-wolf-aa (pod-1) will not start: status FAILED (seen: PENDING → FAILED)",
                pod_id="pod-1", pod=_pod("FAILED", None), status="FAILED", history=["PENDING", "FAILED"],
            )

    result = _run_up(monkeypatch, _Wait)

    assert result.exit_code == EXIT_API_ERROR, result.output
    assert "eager-wolf-aa" in result.output and "pod-1" in result.output
    assert "FAILED" in result.output
    assert "lium rm eager-wolf-aa" in result.output


def test_up_ready_timeout_is_forwarded_and_names_the_billing_pod(monkeypatch):
    seen = {}

    class _Wait:
        def execute(self, ctx):
            seen["timeout"] = ctx.get("timeout")
            return ActionResult(ok=False, data={}, error="still starting")

    result = _run_up(monkeypatch, _Wait, ["--ready-timeout", "90"])

    assert seen["timeout"] == 90
    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "still starting after 90s" in result.output
    assert "pod-1" in result.output and "lium rm train" in result.output


def test_up_ready_timeout_with_ttl_names_the_removal_time(monkeypatch):
    # DAH-3331: --ttl/--until are scheduled at the rent; when the wait gives up the caller learns
    # the pod still has its end time (before, it had none and the message said so). Details in
    # test_up_ttl_at_rent.py.
    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=False, data={}, error="still starting")

    result = _run_up(monkeypatch, _Wait, ["--ready-timeout", "90", "--ttl", "2h"])

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "Auto-termination is scheduled for" in " ".join(result.output.split())

    # DAH-2884: a --budget cap is computed from the ready pod, so it is not set yet when the wait gives up.
    result = _run_up(monkeypatch, _Wait, ["--ready-timeout", "90", "--budget", "5"])

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    text = " ".join(result.output.split())
    assert "The --budget cap was NOT scheduled (it is computed from the ready pod)" in text
    assert "lium rm train --in <duration>" in text

    result = _run_up(monkeypatch, _Wait, ["--ready-timeout", "90"])

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "Auto-termination" not in result.output


def test_up_bounds_the_wait_by_the_default_budget(monkeypatch):
    # DAH-2931: the wait is no longer unbounded by default; --timeout (900 s) is what it gets.
    seen = {}

    class _Wait:
        def execute(self, ctx):
            seen["timeout"] = ctx.get("timeout", "unset")
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    result = _run_up(monkeypatch, _Wait)

    assert result.exit_code == 0, result.output
    assert 0 < seen["timeout"] <= up_command.DEFAULT_TIMEOUT_SECONDS


def test_wait_ready_action_reports_a_timeout_as_not_ok():
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval, on_poll=None):
            assert (pod_id, timeout) == ("pod-1", 5)
            return None

    result = up_actions.WaitReadyAction().execute({"lium": _Lium(), "pod_id": "pod-1", "timeout": 5})

    assert result.ok is False
    assert "pod-1" in result.error


def test_wait_ready_action_lets_a_start_error_through():
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval, on_poll=None):
            raise PodStartError("dead", pod_id=pod_id, status="FAILED")

    with pytest.raises(PodStartError):
        up_actions.WaitReadyAction().execute({"lium": _Lium(), "pod_id": "pod-1"})


def test_up_help_documents_ready_timeout():
    result = CliRunner().invoke(up_command.up_command, ["--help"])

    assert result.exit_code == 0
    assert "--ready-timeout" in result.output


def _run_up_with_prompt(monkeypatch, *, answer_takes: float, resolve_takes: float = 0.0, timeout: int = 60):
    """`lium up` without --yes on a fake clock: `resolve_takes` seconds pass while the node is found,
    `answer_takes` seconds pass at the confirm prompt (answered yes)."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(up_command.time, "monotonic", lambda: clock["now"])
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def get_deployment_estimate(self, *a, **k):
            return {}

    class _Resolve:
        def execute(self, ctx):
            clock["now"] += resolve_takes
            return ActionResult(ok=True, data={"executor": executor})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    rented = []

    class _Rent:
        def execute(self, ctx):
            rented.append(ctx["executor"].huid)
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "train"})

    class _Wait:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod": _pod("RUNNING")})

    def confirm(message):
        clock["now"] += answer_takes
        return True

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", _Wait)
    monkeypatch.setattr(up_command.ui, "confirm", confirm)
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--no-ssh", "--timeout", str(timeout)])
    return result, rented


def test_a_slow_answer_at_the_prompt_does_not_eat_the_timeout_budget(monkeypatch):
    # the prompt waits on a person: five minutes of thinking against a 60 s budget still rents
    result, rented = _run_up_with_prompt(monkeypatch, answer_takes=300.0)

    assert result.exit_code == 0, result.output
    assert rented == ["brave-fox-3a"]
    assert "timeout_before_rent" not in result.output


def test_time_spent_before_the_prompt_still_counts_against_the_budget(monkeypatch):
    # the negative control: the same five minutes spent finding the node runs the budget out
    result, rented = _run_up_with_prompt(monkeypatch, answer_takes=0.0, resolve_takes=300.0)

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert rented == []
    assert "ran out before renting brave-fox-3a; no pod was created" in " ".join(result.output.split())
