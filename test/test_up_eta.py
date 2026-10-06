"""DAH-3005: the backend's estimated time-to-ready and creation phase reach the SDK and `lium up`.

Measured (7 d to 6 Sep 2026): cached template p50 22.5 s / p90 51 s, uncached p50 48 s / p90
301 s. The API sends ``estimated_ready_seconds`` / ``eta_basis`` / ``phase`` on a PENDING pod;
``PodInfo`` carries them, ``PodInfo.eta_hint()`` renders them, and the ``lium up`` progress
line and timeout message use that.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch


from lium.cli.up import actions as up_actions
from lium.cli.utils import EXIT_GENERAL_ERROR
from lium.sdk import Config, Lium, PodInfo

from test_pod_start_error import _pod
from test_up_progress_timeout import _flat, _run_up

# --- SDK ------------------------------------------------------------------------------------


def _pending(seconds, phase="pulling image", basis="executor_history") -> PodInfo:
    return replace(_pod("PENDING", None), estimated_ready_seconds=seconds, eta_basis=basis, phase=phase)


def test_ps_maps_the_eta_fields_and_leaves_them_none_on_an_older_backend():
    client = Lium(Config(api_key="test"))
    payload = [
        {"id": "pod-1", "pod_name": "a", "status": "PENDING", "estimated_ready_seconds": 18,
         "eta_basis": "class_median", "phase": "creating volume"},
        {"id": "pod-2", "pod_name": "b", "status": "RUNNING"},
    ]

    with patch.object(Lium, "_request") as request:
        request.return_value.json.return_value = payload
        pending, running = client.ps()

    assert (pending.estimated_ready_seconds, pending.eta_basis, pending.phase) == (18, "class_median", "creating volume")
    assert (running.estimated_ready_seconds, running.eta_basis, running.phase) == (None, None, None)


def test_eta_hint_renders_seconds_minutes_overrun_and_phase():
    assert _pending(18).eta_hint() == "est. ready in ~18 s (phase: pulling image)"
    assert _pending(150).eta_hint() == "est. ready in ~2 min (phase: pulling image)"
    assert _pending(0, phase="configuring ssh").eta_hint() == "est. ready any moment now (phase: configuring ssh)"
    assert _pending(25, phase=None).eta_hint() == "est. ready in ~25 s"
    assert _pending(None, phase="queued").eta_hint() == "phase: queued"


def test_eta_hint_is_none_without_an_estimate_or_a_phase():
    assert _pod("PENDING", None).eta_hint() is None
    assert _pod("RUNNING").eta_hint() is None


# --- CLI ------------------------------------------------------------------------------------


def test_progress_line_carries_the_estimate_and_reprints_when_the_phase_moves():
    lines: list[str] = []
    on_poll = up_actions.WaitReadyAction()._progress(lines.append)

    on_poll(_pending(20, phase="connecting to node"), "PENDING", 2)
    on_poll(_pending(18, phase="connecting to node"), "PENDING", 4)  # same phase, too soon: silent
    on_poll(_pending(15, phase="pulling image"), "PENDING", 6)
    on_poll(_pending(0, phase="configuring ssh"), "PENDING", 24)

    assert lines == [
        "waiting for eager-wolf-aa… PENDING (2 s) · est. ready in ~20 s (phase: connecting to node)",
        "waiting for eager-wolf-aa… PENDING (6 s) · est. ready in ~15 s (phase: pulling image)",
        "waiting for eager-wolf-aa… PENDING (24 s) · est. ready any moment now (phase: configuring ssh)",
    ]


def test_progress_line_is_unchanged_when_the_backend_sends_no_estimate():
    lines: list[str] = []
    on_poll = up_actions.WaitReadyAction()._progress(lines.append)

    on_poll(_pod("PENDING", None), "PENDING", 3)

    assert lines == ["waiting for eager-wolf-aa… PENDING (3 s)"]


def test_timeout_error_says_what_the_backend_still_expects():
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval=None, on_poll=None):
            on_poll(_pending(40, phase="pulling image", basis="cold_pull_estimate"), "PENDING", timeout)
            return None

    result = up_actions.WaitReadyAction().execute(
        {"lium": _Lium(), "pod_id": "pod-1", "timeout": 30, "report": lambda _line: None}
    )

    assert result.ok is False
    assert result.error == "Pod pod-1 was still starting after 30s (backend: est. ready in ~40 s (phase: pulling image))"
    assert result.data == {"eta_hint": "est. ready in ~40 s (phase: pulling image)"}


def test_timeout_error_is_the_plain_one_without_an_estimate():
    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval=None, on_poll=None):
            on_poll(_pod("PENDING", None), "PENDING", timeout)
            return None

    result = up_actions.WaitReadyAction().execute(
        {"lium": _Lium(), "pod_id": "pod-1", "timeout": 30, "report": lambda _line: None}
    )

    assert result.error == "Pod pod-1 was still starting after 30s"
    assert result.data == {"eta_hint": None}


def test_lium_up_timeout_message_carries_the_backends_estimate(monkeypatch):
    """The whole path: wait_ready's last poll → WaitReadyAction → the `lium up` error the caller reads."""

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def wait_ready(self, pod_id, *, timeout, poll_interval=None, on_poll=None):
            on_poll(_pending(40, phase="pulling image", basis="cold_pull_estimate"), "PENDING", timeout)
            return None

    class _Wait(up_actions.WaitReadyAction):
        def execute(self, ctx):
            return super().execute({**ctx, "lium": _Lium()})

    result = _run_up(monkeypatch, wait_action=_Wait, args=["--timeout", "120"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    output = _flat(result.output)
    assert "Pod train (id: pod-1) is still starting after" in output
    assert "and is billing (backend: est. ready in ~40 s (phase: pulling image))." in output
    assert "lium rm train" in output
