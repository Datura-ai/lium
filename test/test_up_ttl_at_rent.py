"""DAH-3331: `lium up --ttl/--until` is scheduled the moment the rent returns a pod id, not once the pod is ready.

Before, the schedule was set after the wait for RUNNING. A rent whose wait ran out (`--timeout`,
`--ready-timeout`) or whose pod never came up left a billing pod with no end time. Now the id the
rent returns is enough: the backend's removal task takes a pod in any status but DELETING.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.up import command as up_command
from lium.cli.up import parsing
from lium.cli.utils import EXIT_API_ERROR, EXIT_GENERAL_ERROR
from lium.sdk import LiumError, LiumServerError, PodInfo, PodStartError


def _ready_pod() -> PodInfo:
    return PodInfo(
        id="pod-1", name="train", status="RUNNING", huid="eager-wolf-aa",
        ssh_cmd="ssh user@pod.example -p 20299", ports={}, created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z", executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None, gpu_count=1,
    )


class _Wait:
    """`WaitReadyAction` scripted: ``"ready"``, ``"timeout"`` (still starting) or ``"failed"`` (dead pod)."""

    outcome = "ready"
    calls: list

    def execute(self, ctx):
        _Wait.calls.append(("wait", ctx["pod_id"]))
        if _Wait.outcome == "timeout":
            return ActionResult(ok=False, data={}, error="still starting")
        if _Wait.outcome == "failed":
            raise PodStartError(
                "Pod eager-wolf-aa (pod-1) will not start: status FAILED (seen: PENDING → FAILED)",
                pod_id="pod-1", pod=_ready_pod(), status="FAILED", history=["PENDING", "FAILED"],
            )
        return ActionResult(ok=True, data={"pod": _ready_pod()})


def _run_up(monkeypatch, args=(), *, wait="ready", rent_error=None, schedule_errors=(), on_rent=None):
    """`lium up brave-fox-3a -y --no-ssh <args>` against fakes; returns (result, calls).

    ``calls`` is the order of SDK-level events: ``("rent",)``, ``("schedule", pod, iso_time)``,
    ``("wait", pod_id)``. ``schedule_errors`` are raised by successive ``schedule_termination``
    calls (None = succeed); after the list runs out the call succeeds. ``on_rent`` runs inside
    the rent, before it returns.
    """
    calls: list = []
    errors = list(schedule_errors)
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)   # a server without workspaces: no context line

        def get_deployment_estimate(self, *a, **k):
            return {}

        def schedule_termination(self, pod, *, termination_time):
            calls.append(("schedule", pod, termination_time))
            error = errors.pop(0) if errors else None
            if error is not None:
                raise error
            return {"removal_scheduled_at": termination_time}

    class _Resolve:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _Template:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})

    class _Rent:
        def execute(self, ctx):
            calls.append(("rent",))
            if rent_error is not None:
                raise rent_error
            if on_rent is not None:
                on_rent()
            return ActionResult(ok=True, data={"pod_info": {"id": "pod-1"}, "pod_id": "pod-1", "pod_name": "train"})

    class _Verify:
        def execute(self, ctx):
            return ActionResult(ok=True, data={})

    _Wait.outcome, _Wait.calls = wait, calls
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _Resolve)
    monkeypatch.setattr(up_command, "ResolveTemplateAction", _Template)
    monkeypatch.setattr(up_command, "RentPodAction", _Rent)
    monkeypatch.setattr(up_command, "WaitReadyAction", _Wait)
    monkeypatch.setattr(up_command, "VerifyGpuCountAction", _Verify)
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--yes", "--no-ssh", *args])
    return result, calls


def _kinds(calls):
    return [c[0] for c in calls]


def _flat(result):
    return " ".join(result.output.split())


def test_ttl_is_scheduled_with_the_rented_id_before_the_wait(monkeypatch):
    before = datetime.now(timezone.utc)
    result, calls = _run_up(monkeypatch, ["--ttl", "2h"])
    after = datetime.now(timezone.utc)

    assert result.exit_code == 0, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait"], calls
    _, pod, when = calls[1]
    assert pod == "pod-1", "the id the rent returned, not a PodInfo the wait would have produced"
    scheduled = datetime.fromisoformat(when)
    assert before + timedelta(hours=2) <= scheduled <= after + timedelta(hours=2)
    assert "removal of pod train scheduled for" in _flat(result) and "whether or not it becomes ready" in _flat(result)
    assert "NOT scheduled" not in result.output


def test_ttl_counts_from_the_rent_not_from_parsing_the_flag(monkeypatch):
    # Finding the node and answering the prompt happen between parsing --ttl and the rent; the
    # two hours the caller asked for start when the pod exists and starts billing. The command's
    # clock reads T0 until the rent, which moves it ten minutes on: a time computed anywhere
    # before the rent would land at T0 + 2h.
    t0 = datetime.now(timezone.utc)
    clock = {"now": t0}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    def rent_takes_ten_minutes():
        clock["now"] = t0 + timedelta(minutes=10)

    monkeypatch.setattr(up_command, "datetime", _Clock)
    result, calls = _run_up(monkeypatch, ["--ttl", "2h"], on_rent=rent_takes_ten_minutes)

    assert result.exit_code == 0, result.output
    assert datetime.fromisoformat(calls[1][2]) == t0 + timedelta(minutes=10, hours=2)


def test_ttl_holds_when_the_wait_runs_out(monkeypatch):
    result, calls = _run_up(monkeypatch, ["--ready-timeout", "90", "--ttl", "2h"], wait="timeout")

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait"], calls
    text = _flat(result)
    assert "still starting after 90s" in text and "pod-1" in text
    assert "Auto-termination is scheduled for" in text
    assert "removed then even if it never becomes ready" in text
    assert "NOT scheduled" not in text


def test_ttl_holds_when_the_pod_fails_to_start(monkeypatch):
    result, calls = _run_up(monkeypatch, ["--ttl", "2h"], wait="failed")

    assert result.exit_code == EXIT_API_ERROR, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait"], calls
    text = _flat(result)
    assert "failed to start" in text and "FAILED" in text
    assert "Auto-termination is scheduled for" in text
    assert "lium rm eager-wolf-aa" in text


def test_nothing_is_scheduled_when_the_rent_fails(monkeypatch):
    result, calls = _run_up(monkeypatch, ["--ttl", "2h"], rent_error=LiumError("Executor is not available"))

    assert result.exit_code == EXIT_API_ERROR, result.output
    assert _kinds(calls) == ["rent"], calls
    assert "could not be rented" in _flat(result)


def test_without_ttl_or_until_nothing_is_scheduled(monkeypatch):
    result, calls = _run_up(monkeypatch)

    assert result.exit_code == 0, result.output
    assert _kinds(calls) == ["rent", "wait"], calls
    assert "termination" not in result.output.lower()

    result, calls = _run_up(monkeypatch, ["--ready-timeout", "90"], wait="timeout")

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert "Auto-termination" not in result.output


def test_until_is_scheduled_at_rent_with_the_absolute_time(monkeypatch):
    result, calls = _run_up(monkeypatch, ["--until", "2099-01-01 12:00"])

    assert result.exit_code == 0, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait"], calls
    # `--until` is a point in time (parsed in the local zone): it is not moved to "rent + something"
    expected, error = parsing.parse_time_spec("2099-01-01 12:00")
    assert not error
    assert datetime.fromisoformat(calls[1][2]) == expected


def test_a_schedule_that_fails_after_the_rent_is_tried_again_once_the_pod_is_ready(monkeypatch):
    result, calls = _run_up(
        monkeypatch, ["--ttl", "2h"], schedule_errors=[LiumServerError("Server error: 502")]
    )

    assert result.exit_code == 0, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait", "schedule"], calls
    assert calls[1][2] == calls[3][2], "the retry keeps the time set at the rent, it does not extend the TTL"
    assert calls[3][1] is not calls[1][1] and calls[3][1].id == "pod-1"  # the ready PodInfo this time
    text = _flat(result)
    assert "was NOT scheduled (Server error: 502); it is tried again once the pod is ready" in text
    assert text.index("was NOT scheduled") < text.index("removal of pod train scheduled for"), "the retry's success is said too"


def test_a_schedule_that_fails_after_the_rent_and_a_wait_that_runs_out_say_so(monkeypatch):
    result, calls = _run_up(
        monkeypatch, ["--ready-timeout", "90", "--ttl", "2h"], wait="timeout",
        schedule_errors=[LiumServerError("Server error: 502")],
    )

    assert result.exit_code == EXIT_GENERAL_ERROR, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait"], calls
    text = _flat(result)
    assert "Auto-termination (--ttl/--until) was NOT scheduled (the schedule call failed after the rent)" in text
    assert "lium rm train --in <duration>" in text
    assert "is scheduled for" not in text


def test_a_schedule_that_fails_twice_ends_the_command_naming_the_ready_pod(monkeypatch):
    result, calls = _run_up(
        monkeypatch, ["--ttl", "2h"],
        schedule_errors=[LiumServerError("Server error: 502"), LiumServerError("Server error: 502")],
    )

    assert result.exit_code != 0, result.output
    assert _kinds(calls) == ["rent", "schedule", "wait", "schedule"], calls
    text = _flat(result)
    assert "is running but auto-termination was NOT scheduled" in text
    assert "eager-wolf-aa" in text and "pod-1" in text


@pytest.mark.parametrize("pod", ["pod-uuid-1", {"id": "pod-uuid-1", "status": "PENDING"}, SimpleNamespace(id="pod-uuid-1")])
def test_sdk_schedule_termination_takes_the_id_the_rent_returned(monkeypatch, pod):
    # `Lium.up()` returns a dict (or just an id) long before `wait_ready()` has a PodInfo; all three
    # forms post to the same pod.
    from lium.sdk import Config, Lium
    from lium.sdk import client as client_module

    calls = []

    class _Ok:
        ok, status_code = True, 200

        def json(self):
            return {"removal_scheduled_at": "2026-09-09T00:00:00Z"}

    def fake_request(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        return _Ok()

    monkeypatch.setattr(client_module.requests, "request", fake_request)
    client = Lium(Config(api_key="test"))

    client.schedule_termination(pod, termination_time="2026-09-09T00:00:00Z")

    assert len(calls) == 1
    method, url, payload = calls[0]
    assert method == "POST" and url.endswith("/pods/pod-uuid-1/schedule-removal")
    assert payload == {"removal_scheduled_at": "2026-09-09T00:00:00Z"}


def test_help_says_the_schedule_holds_before_the_pod_is_ready():
    result = CliRunner().invoke(up_command.up_command, ["--help"])

    assert result.exit_code == 0
    text = _flat(result)
    sentence = "Scheduled as soon as the pod exists, so it holds even if the pod never becomes ready."
    assert text.count(sentence) == 2, "once for --ttl, once for --until"
    assert text.index("--ttl TEXT") < text.index(sentence) < text.index("--until TEXT") < text.rindex(sentence)
