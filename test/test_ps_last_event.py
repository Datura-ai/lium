"""DAH-2932 — `lium ps <pod>` shows why one pod is REBOOT_FAILED / BROKEN, and a pod that is not
listed points at `lium describe <id>` for its events."""

import json
from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ps import command as ps_module
from lium.cli.utils import EXIT_POD_NOT_FOUND
from lium.sdk import ExecutorInfo, LiumNotFoundError, PodInfo

LAST_EVENT = {
    "created_at": "2026-09-06T00:20:28",
    "event_type": "pod-lifecycle",
    "sub_event_type": "pod-lifecycle.status",
    "from_status": "REBOOT_PENDING",
    "to_status": "REBOOT_FAILED",
    "reason": "reboot_failed",
    "detail": "Container creation failed due to Failed create_container (failure_step: ssh_connect)",
    "error": None,
}


def _pod(status: str = "REBOOT_FAILED") -> PodInfo:
    return PodInfo(
        id="pod-uuid-1",
        name="train",
        status=status,
        huid="eager-wolf-aa",
        ssh_cmd="ssh user@pod.example -p 34567",
        ports={"22": 34567},
        created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z",
        executor=ExecutorInfo(
            id="executor-uuid-1", huid="brave-otter-11", machine_name="NVIDIA H100 SXM 1x", gpu_type="H100",
            gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={}, specs={}, status="active",
            docker_in_docker=False, ip="192.0.2.10",
        ),
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


class _FakeLium:
    pods: list = []
    detail: dict = {}
    detail_calls = 0
    # `Lium.workspaces` on a server without workspaces: `ps` reads it for its workspace line (DAH-3033)
    workspaces = SimpleNamespace(current=lambda: None)

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def pod(self, pod_id):
        _FakeLium.detail_calls += 1
        if isinstance(self.detail, Exception):
            raise self.detail
        return dict(self.detail)


def _run_ps(monkeypatch, pods, args, detail=None):
    _FakeLium.pods = pods
    _FakeLium.detail = detail if detail is not None else {}
    _FakeLium.detail_calls = 0
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    monkeypatch.setattr(ps_module, "Lium", _FakeLium)
    return CliRunner().invoke(cli, ["ps", *args])


def test_ps_of_one_pod_prints_its_last_event(monkeypatch):
    result = _run_ps(monkeypatch, [_pod()], ["eager-wolf-aa"], detail={"last_event": LAST_EVENT})

    assert result.exit_code == 0, result.output
    output = " ".join(result.output.split())
    assert "last event: REBOOT_FAILED (reboot_failed) — Container creation failed" in output
    assert "failure_step: ssh_connect" in output


def test_ps_of_one_pod_in_json_carries_last_event(monkeypatch):
    result = _run_ps(monkeypatch, [_pod()], ["eager-wolf-aa", "--format", "json"], detail={"last_event": LAST_EVENT})

    assert result.exit_code == 0, result.output
    [row] = json.loads(result.stdout)
    assert row["last_event"]["to_status"] == "REBOOT_FAILED"
    assert row["last_event"]["reason"] == "reboot_failed"


def test_ps_without_a_target_makes_no_detail_call(monkeypatch):
    result = _run_ps(monkeypatch, [_pod(), _pod("RUNNING")], ["--format", "json"], detail={"last_event": LAST_EVENT})

    assert result.exit_code == 0, result.output
    assert _FakeLium.detail_calls == 0
    assert all("last_event" not in row for row in json.loads(result.stdout))


def test_ps_of_one_pod_survives_a_failing_detail_call(monkeypatch):
    result = _run_ps(monkeypatch, [_pod()], ["eager-wolf-aa", "--format", "json"], detail=LiumNotFoundError("gone"))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["last_event"] is None


def test_ps_of_one_pod_survives_a_transport_error_on_the_detail_call(monkeypatch):
    """`Lium.pod()` re-raises requests errors after its retries as-is (not a LiumError); the listing already answered."""
    import requests

    result = _run_ps(monkeypatch, [_pod()], ["eager-wolf-aa", "--format", "json"], detail=requests.ConnectionError("reset"))

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["last_event"] is None


def test_describe_display_imports_on_its_own():
    """The describe and ps packages import each other's display helpers; a module-level import from
    ps.command back into describe made `import lium.cli.describe.display` fail while partially initialised."""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", "import lium.cli.describe.display; import lium.cli.ps.command"],
        capture_output=True, text=True, env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert proc.returncode == 0, proc.stderr


def test_ps_of_an_unlisted_pod_points_at_describe(monkeypatch):
    result = _run_ps(monkeypatch, [_pod()], ["no-such-pod"])

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert "lium describe <pod id>" in " ".join(result.output.split())
