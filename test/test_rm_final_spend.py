"""DAH-2982: `lium rm` reports what the pod it removes cost.

`rm` lists the pods — with their $/h and start time — right before deleting
them, then printed only `Removed 1 pod(s): <huid>`; renters rebuilt the spend
from observed $/h × wall time afterwards.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.rm import command as rm_module
from lium.cli.rm import display

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _pod(price=0.58, age=timedelta(hours=1, minutes=32), created_at=None, name="train-pod"):
    if created_at is None:
        # The API returns naive UTC timestamps like 2026-09-06T10:28:00.123456.
        created_at = (NOW - age).replace(tzinfo=None).isoformat()
    executor = SimpleNamespace(price_per_hour=price) if price is not None else None
    return SimpleNamespace(id="pod-uuid-1", huid="eager-wolf-aa", name=name, executor=executor, created_at=created_at)


def _run_rm(monkeypatch, pod, *args, workspace=None):
    calls = {"rm": [], "scheduled": []}

    class _FakeLium:
        # a server without workspaces by default: `rm` reads it for its workspace line (lium#183);
        # `workspace=` makes it a workspace server whose key acts in that workspace
        workspaces = SimpleNamespace(current=lambda: workspace)
        config = SimpleNamespace(workspace=None, workspace_id=None, workspace_explicit=False)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [pod]

        def rm(self, pod):
            calls["rm"].append(pod.huid)

        def schedule_termination(self, pod, termination_time=None):
            calls["scheduled"].append(termination_time)

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)
    monkeypatch.setattr(rm_module, "datetime", _FrozenDatetime)
    return CliRunner().invoke(cli, ["rm", "train-pod", "-y", *args]), calls


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


def test_pod_spend_is_uptime_times_list_price():
    spend = display.pod_spend(_pod(), now=NOW)

    assert spend == {
        "uptime": "1.5h",
        "uptime_hours": 1.53,
        "price_per_hour": 0.58,
        "spent_usd": 0.89,
        "spent_is_estimate": True,
    }


def test_uptime_is_spelled_like_ps_json():
    """`uptime` means one thing across `ps --format json`, `describe` and `rm`."""
    from lium.cli.ps import display as ps_display

    assert display.pod_spend(_pod(age=timedelta(minutes=32)), now=NOW)["uptime"] == "32m"
    assert display.pod_spend(_pod(age=timedelta(hours=30)), now=NOW)["uptime"] == "1.2d"
    assert display.pod_spend(_pod(), now=NOW)["uptime"] == ps_display.format_duration(92 * 60)


def test_a_pending_pod_has_spent_nothing():
    """The same rule as `lium spend`: PENDING has not started billing, whatever its age."""
    pod = _pod()
    pod.status = "PENDING"

    spend = display.pod_spend(pod, now=NOW)

    assert (spend["spent_usd"], spend["price_per_hour"], spend["uptime"]) == (0.0, 0.58, "1.5h")


def test_pod_spend_marks_what_it_cannot_know():
    assert display.pod_spend(_pod(price=None), now=NOW)["spent_usd"] is None
    assert display.pod_spend(_pod(price=0.0), now=NOW)["price_per_hour"] is None
    assert display.pod_spend(_pod(created_at="not-a-date"), now=NOW)["uptime"] is None
    assert display.pod_spend(_pod(created_at=""), now=NOW)["uptime_hours"] is None


def test_rm_prints_the_final_spend_line(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod())

    assert result.exit_code == 0, result.output
    assert calls["rm"] == ["eager-wolf-aa"]
    assert "Removed 1 pod(s): eager-wolf-aa" in result.output
    assert "removed train-pod — 1.5h at $0.58/h ≈ $0.89" in result.output


def test_rm_says_when_it_has_no_price(monkeypatch):
    result, _ = _run_rm(monkeypatch, _pod(price=None, age=timedelta(hours=30)))

    assert result.exit_code == 0, result.output
    assert "removed train-pod — 1.2d (no $/h on record)" in result.output


def test_rm_json_carries_the_spend_fields(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod(), "--format", "json")

    assert result.exit_code == 0, result.output
    assert calls["rm"] == ["eager-wolf-aa"]
    payload = json.loads(result.output)
    assert payload["failed"] == []
    assert payload["removed"] == [{
        "id": "pod-uuid-1",
        "huid": "eager-wolf-aa",
        "name": "train-pod",
        "uptime": "1.5h",
        "uptime_hours": 1.53,
        "price_per_hour": 0.58,
        "spent_usd": 0.89,
        "spent_is_estimate": True,
    }]


def test_rm_json_on_a_workspace_server_keeps_stdout_one_document(monkeypatch):
    """On a server with workspaces (main since lium#183) `rm` prints a `Workspace: …` context line before it acts.
    With --format json that line must go to stderr: `lium rm … --format json | jq` reads stdout alone, and a
    prose line before the payload is a parse error. A rm that printed the line to stdout fails this test."""
    from lium.sdk.models import WorkspaceInfo

    research = WorkspaceInfo(id="ws-1", name="Research", role="owner", billing_owner_user_id="u-1")
    runner_result, calls = _run_rm(monkeypatch, _pod(), "--format", "json", workspace=research)

    assert runner_result.exit_code == 0, runner_result.output
    payload = json.loads(runner_result.stdout)   # stdout alone must parse
    assert [p["huid"] for p in payload["removed"]] == ["eager-wolf-aa"]
    assert "Workspace: Research" in runner_result.stderr and "Workspace" not in runner_result.stdout


def test_rm_json_with_a_failure_stays_parseable_and_exits_non_zero(monkeypatch):
    """A partial failure must not append a second message to stdout after the payload."""
    from lium.cli.utils import EXIT_GENERAL_ERROR

    calls = {"rm": []}

    class _FakeLium:
        # a server without workspaces: `rm` reads it for its workspace line (lium#183)
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [_pod()]

        def rm(self, pod):
            calls["rm"].append(pod.huid)
            raise RuntimeError("executor unreachable")

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)
    monkeypatch.setattr(rm_module, "datetime", _FrozenDatetime)

    result = CliRunner().invoke(cli, ["rm", "train-pod", "-y", "--format", "json"])

    assert result.exit_code == EXIT_GENERAL_ERROR
    payload = json.loads(result.stdout)
    assert payload == {"removed": [], "failed": ["eager-wolf-aa"]}


def test_rm_by_row_number_with_json_keeps_stdout_one_document(monkeypatch, tmp_path):
    """The "Pod 1 → huid" line a row number earns (DAH-2559) goes to stderr under --format json."""
    from lium.cli import utils

    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    pod = _pod()
    utils.store_pod_selection([pod], now=NOW)
    calls = {"rm": []}

    class _FakeLium:
        # a server without workspaces: `rm` reads it for its workspace line (lium#183)
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return [pod]

        def rm(self, pod):
            calls["rm"].append(pod.huid)

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)
    monkeypatch.setattr(rm_module, "datetime", _FrozenDatetime)
    from lium.cli.rm import parsing

    monkeypatch.setattr(parsing, "resolve_targets", lambda *a, **k: utils.resolve_targets(*a, now=NOW, **k))

    result = CliRunner().invoke(cli, ["rm", "1", "-y", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert calls["rm"] == ["eager-wolf-aa"]
    assert json.loads(result.stdout)["removed"][0]["huid"] == "eager-wolf-aa"
    assert "Pod 1" in result.stderr


def test_rm_all_on_an_empty_account_with_json_prints_an_empty_payload(monkeypatch):
    class _FakeLium:
        # a server without workspaces: `rm` reads it for its workspace line (lium#183)
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ps(self):
            return []

    monkeypatch.setattr(rm_module, "Lium", _FakeLium)

    result = CliRunner().invoke(cli, ["rm", "--all", "-y", "--format", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"removed": [], "failed": []}


def test_scheduled_removal_reports_scheduled_not_removed(monkeypatch):
    result, calls = _run_rm(monkeypatch, _pod(), "--in", "2h", "--format", "json")

    assert result.exit_code == 0, result.output
    assert calls["rm"] == [] and len(calls["scheduled"]) == 1
    payload = json.loads(result.output)
    assert "removed" not in payload
    assert payload["scheduled"][0]["spent_usd"] == 0.89
    assert payload["termination_time"] == calls["scheduled"][0]

    result, _ = _run_rm(monkeypatch, _pod(), "--in", "2h")
    assert "Scheduled removal for 1 pod(s)" in result.output
    assert "≈" not in result.output
