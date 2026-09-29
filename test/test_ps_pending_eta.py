"""`lium ps` shows the backend's start estimate and phase for a PENDING pod, worded as `lium up` words it."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ps import command as ps_module
from lium.cli.ps import display as ps_display
from lium.sdk import ExecutorInfo, PodInfo


def _pod(status="PENDING", huid="eager-wolf-aa", estimated_ready_seconds=None, phase=None) -> PodInfo:
    return PodInfo(
        id=f"{huid}-uuid",
        name="train",
        status=status,
        huid=huid,
        ssh_cmd="ssh root@pod.invalid -p 2222",
        ports={"22": 2222},
        created_at="2026-09-27T05:00:00Z",
        updated_at="2026-09-27T05:00:00Z",
        executor=ExecutorInfo(
            id="executor-uuid-1", huid="brave-otter-11", machine_name="NVIDIA H100 SXM 1x", gpu_type="H100",
            gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={}, specs={}, status="active",
            docker_in_docker=False, ip="203.0.113.10",
        ),
        template={"name": "Pytorch"},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
        estimated_ready_seconds=estimated_ready_seconds,
        phase=phase,
    )


class _FakeLium:
    pods: list = []
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)


@pytest.fixture
def run_ps(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    monkeypatch.setattr(ps_module, "Lium", _FakeLium)

    def run(pods, *args):
        _FakeLium.pods = pods
        return CliRunner().invoke(cli, ["ps", "--wide", *args])

    return run


def _flat(output: str) -> str:
    return " ".join(output.split())


def test_table_shows_the_estimate_and_phase_of_a_pending_pod(run_ps):
    result = run_ps([_pod(estimated_ready_seconds=18, phase="pulling image")])

    assert result.exit_code == 0, result.output
    assert "eager-wolf-aa PENDING · est. ready in ~18 s (phase: pulling image)" in _flat(result.output)


def test_table_shows_the_phase_alone_when_the_backend_sent_no_estimate(run_ps):
    result = run_ps([_pod(phase="queued")])

    assert result.exit_code == 0, result.output
    assert "eager-wolf-aa PENDING · phase: queued" in _flat(result.output)


def test_table_adds_no_line_for_a_pending_pod_without_estimate_or_phase(run_ps):
    result = run_ps([_pod()])

    assert result.exit_code == 0, result.output
    assert "PENDING ·" not in result.output
    assert "est. ready" not in result.output


def test_table_leaves_a_running_pod_as_it_was(run_ps):
    # a RUNNING pod carrying stale estimate fields still reads as before
    result = run_ps([_pod(status="RUNNING", estimated_ready_seconds=18, phase="pulling image")])

    assert result.exit_code == 0, result.output
    assert "est. ready" not in result.output
    assert "phase:" not in result.output


def test_table_lists_one_line_per_pending_pod(run_ps):
    pods = [
        _pod(huid="eager-wolf-aa", estimated_ready_seconds=300, phase="pulling image"),
        _pod(status="RUNNING", huid="calm-bear-bb"),
        _pod(huid="swift-hawk-cc", estimated_ready_seconds=0),
    ]
    result = run_ps(pods)

    assert result.exit_code == 0, result.output
    output = _flat(result.output)
    assert "eager-wolf-aa PENDING · est. ready in ~5 min (phase: pulling image)" in output
    assert "swift-hawk-cc PENDING · est. ready any moment now" in output
    assert "calm-bear-bb PENDING" not in output


def test_table_escapes_markup_in_the_phase(run_ps):
    result = run_ps([_pod(estimated_ready_seconds=18, phase="[bold]pulling[/bold]")])

    assert result.exit_code == 0, result.output
    assert "(phase: [bold]pulling[/bold])" in _flat(result.output)


def test_json_carries_eta_hint_on_a_pending_row(run_ps):
    result = run_ps([_pod(estimated_ready_seconds=18, phase="pulling image"), _pod(huid="swift-hawk-cc")], "--format", "json")

    assert result.exit_code == 0, result.output
    first, second = json.loads(result.stdout)
    assert first["eta_hint"] == "est. ready in ~18 s (phase: pulling image)"
    assert second["eta_hint"] is None


def test_json_row_of_a_running_pod_has_no_eta_hint_key():
    row = ps_display.compact_pod(_pod(status="RUNNING", estimated_ready_seconds=18, phase="pulling image"))

    assert "eta_hint" not in row


def test_pending_eta_lines_match_the_up_wording():
    pod = _pod(estimated_ready_seconds=18, phase="pulling image")

    assert ps_display.pending_eta_lines([pod]) == [f"eager-wolf-aa PENDING · {pod.eta_hint()}"]
