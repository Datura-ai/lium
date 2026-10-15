"""`Lium.run_job`: one bounded job that always ends with the pod removed and a terminal status."""

import subprocess
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

from lium.sdk import Config, Lium, LiumError, PodStartError, RentResult
from lium.sdk import bounded_job
from lium.sdk.models import ExecutorInfo, PodInfo


def _executor():
    return ExecutorInfo(
        id="exec-1", huid="node-1", machine_name="NVIDIA H100", gpu_type="H100", gpu_count=1,
        price_per_gpu=2.0, price_per_hour=2.0, location={}, specs={}, status="available",
        docker_in_docker=False, ip="1.2.3.4",
    )


def _pod(name, id="pod-1", status="RUNNING"):
    return PodInfo(
        id=id, name=name, status=status, huid="pod-huid", ssh_cmd="ssh root@1.2.3.4 -p 22",
        ports={}, created_at="", updated_at="", executor=_executor(), template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


@pytest.fixture
def lium(monkeypatch):
    client = Lium(Config(api_key="test"))
    calls = {"deletes": [], "schedules": [], "execs": [], "downloads": [], "uploads": [], "delete_fails": 0}
    client.calls = calls

    def rent(**kwargs):
        calls["rent"] = kwargs
        return RentResult(executor=_executor(), price_per_hour=2.0, pod={"id": "pod-1", "name": kwargs["name"]})

    def request(method, path, **kwargs):
        assert method == "DELETE"
        if calls["delete_fails"]:
            calls["delete_fails"] -= 1
            raise LiumError("Can't remove rent for pod because pod is not running.")
        calls["deletes"].append(path)

    monkeypatch.setattr(client, "rent", rent)
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "schedule_termination",
                        lambda pod, termination_time: calls["schedules"].append((pod, termination_time)))
    monkeypatch.setattr(client, "wait_ready", lambda pod, timeout: _pod(pod["name"]))
    monkeypatch.setattr(client, "upload", lambda pod, local, remote: calls["uploads"].append((local, remote)))
    monkeypatch.setattr(client, "download", lambda pod, remote, local: calls["downloads"].append((remote, local)))
    monkeypatch.setattr(client, "exec", lambda pod, command, env=None, timeout=None: (
        calls["execs"].append((command, timeout)) or {"exit_code": 0, "stdout": "done\n", "stderr": "", "success": True}
    ))
    monkeypatch.setattr(client, "billing_statement", lambda **kw: {"pods": [{"pod_id": "pod-1", "total": 0.0421}]})
    monkeypatch.setattr(client, "ps", lambda: [])
    monkeypatch.setattr(bounded_job.time, "sleep", lambda s: None)
    return client


def _scheduled_at(lium):
    (pod_id, when), = lium.calls["schedules"]
    assert pod_id == "pod-1"
    return datetime.fromisoformat(when.replace("Z", "+00:00"))


def test_run_job_success_runs_copies_removes_and_reports_billed_cost(lium):
    result = lium.run_job(
        command="python train.py --epochs 1", gpu_type="H100", max_cost_usd=10, deadline_s=1800,
        inputs={"train.py": "/root/train.py"}, outputs={"/root/model.pt": "model.pt"}, max_price_per_gpu_hour=3,
    )

    assert result.status == "succeeded" and result.ok and result.exit_code == 0
    assert result.stdout == "done\n"
    assert lium.calls["rent"]["max_price_per_gpu_hour"] == 3 and lium.calls["rent"]["name"] == result.name
    assert lium.calls["uploads"] == [("train.py", "/root/train.py")]
    (command, timeout), = lium.calls["execs"]
    assert command.startswith("timeout -k 10 ") and command.endswith("bash -c 'python train.py --epochs 1'")
    assert int(command.split()[3]) <= 1800 - 120 and timeout <= 1800 - 120 + 30
    assert result.outputs == {"/root/model.pt": "model.pt"}
    assert lium.calls["deletes"] == ["/pods/pod-1"] and result.cleanup == "removed"
    assert result.cost_usd == 0.0421 and result.estimated_cost_usd is not None
    assert result.to_dict()["ok"] is True


def test_run_job_schedules_removal_at_the_deadline_when_the_budget_lasts_longer(lium):
    before = datetime.now(timezone.utc)

    lium.run_job(command="true", gpu_type="H100", max_cost_usd=100, deadline_s=600)

    assert abs((_scheduled_at(lium) - before).total_seconds() - 600) < 5


def test_run_job_schedules_removal_when_the_budget_is_spent_and_reports_budget_exhausted(lium, monkeypatch):
    monkeypatch.setattr(lium, "exec", _exec_raises(TimeoutError("exec timed out")))
    before = datetime.now(timezone.utc)

    # $1 at $2/h lasts 30 minutes, before the 2-hour deadline
    result = lium.run_job(command="sleep 9999", gpu_type="H100", max_cost_usd=1, deadline_s=7200)

    assert abs((_scheduled_at(lium) - before).total_seconds() - 1800) < 5
    assert result.status == "budget_exhausted" and result.cleanup == "removed"


def _exec_locally(pod, command, env=None, timeout=None):
    """The generated `timeout … bash -c …` wrapper, run for real on this machine."""
    run = subprocess.run(["bash", "-c", command], capture_output=True, text=True, timeout=timeout)
    return {"exit_code": run.returncode, "stdout": run.stdout, "stderr": run.stderr}


@pytest.mark.parametrize(
    "command, deadline_s, status, exit_code",
    [("sleep 30", 3, "timed_out", 124), ("exit 124", 600, "failed", 124), ("exit 137", 600, "failed", 137)],
    ids=["wrapper-stopped-it", "command-exits-124", "command-exits-137"],
)
def test_run_job_timeout_status_comes_from_the_wrapper_not_the_exit_code(lium, monkeypatch, command, deadline_s,
                                                                         status, exit_code):
    monkeypatch.setattr(lium, "exec", _exec_locally)

    result = lium.run_job(command=command, gpu_type="H100", max_cost_usd=100, deadline_s=deadline_s,
                          output_reserve_s=1)

    assert (result.status, result.exit_code, result.cleanup) == (status, exit_code, "removed")


def test_run_job_failed_command_still_copies_outputs_and_names_the_missing_one(lium, monkeypatch):
    monkeypatch.setattr(lium, "exec", lambda pod, command, env=None, timeout=None: {"exit_code": 2, "stdout": "", "stderr": "boom"})

    def download(pod, remote, local):
        if remote == "/root/model.pt":
            raise FileNotFoundError("no such file")
        lium.calls["downloads"].append((remote, local))

    monkeypatch.setattr(lium, "download", download)

    result = lium.run_job(command="false", gpu_type="H100", max_cost_usd=5, deadline_s=600,
                          outputs={"/root/train.log": "train.log", "/root/model.pt": "model.pt"})

    assert result.status == "failed" and result.exit_code == 2 and result.stderr == "boom"
    assert result.outputs == {"/root/train.log": "train.log"}
    assert "no such file" in result.missing_outputs["/root/model.pt"]
    assert result.cleanup == "removed"


def test_run_job_rent_failure_removes_a_pod_the_lost_response_created(lium, monkeypatch):
    def rent(**kwargs):
        lium.calls["rent"] = kwargs
        raise LiumError("read timed out")

    monkeypatch.setattr(lium, "rent", rent)
    pods = lambda: [_pod(lium.calls["rent"]["name"]), _pod("someone-elses-pod", id="pod-2")]  # noqa: E731
    monkeypatch.setattr(lium, "ps", lambda: [p for p in pods() if f"/pods/{p.id}" not in lium.calls["deletes"]])

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "rent_failed" and "read timed out" in result.error
    assert lium.calls["deletes"] == ["/pods/pod-1"] and result.cleanup == "removed"


def test_run_job_rent_failure_leaves_a_pre_existing_pod_with_the_same_name(lium, monkeypatch):
    monkeypatch.setattr(lium, "rent", Mock(side_effect=LiumError("no node matches")))
    monkeypatch.setattr(lium, "ps", lambda: [_pod("train")])

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600, name="train")

    assert lium.calls["deletes"] == [] and result.cleanup == "not_rented"
    assert result.name.startswith("train-") and result.name != "train"


def test_run_job_rent_refused_with_no_pod_left_reports_not_rented(lium, monkeypatch):
    monkeypatch.setattr(lium, "rent", lambda **kw: (_ for _ in ()).throw(LiumError("no node matches")))

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "rent_failed" and result.cleanup == "not_rented" and result.pod_id is None
    assert lium.calls["deletes"] == [] and result.cost_usd is None


def test_run_job_boot_failure_removes_the_pod(lium, monkeypatch):
    def wait_ready(pod, timeout):
        raise PodStartError("pod reached FAILED", pod_id="pod-1", status="FAILED")

    monkeypatch.setattr(lium, "wait_ready", wait_ready)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "boot_failed" and lium.calls["execs"] == []
    assert lium.calls["deletes"] == ["/pods/pod-1"]


def test_run_job_ssh_error_reports_error_and_removes_the_pod(lium, monkeypatch):
    def exec_(pod, command, env=None, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(lium, "exec", exec_)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "error" and "connection reset" in result.error
    assert result.cleanup == "removed"


class _OutputLimitExceeded(LiumError):
    """Stands in for lium#314's OutputLimitExceeded, which `Lium.exec` raises past `max_output_bytes`."""


def _exec_exits(code):
    return lambda pod, command, env=None, timeout=None: {"exit_code": code, "stdout": "", "stderr": ""}


def _exec_raises(exc):
    def exec_(pod, command, env=None, timeout=None):
        raise exc

    return exec_


@pytest.mark.parametrize(
    "exec_, status",
    [
        (_exec_exits(0), "succeeded"),
        (_exec_exits(2), "failed"),
        (_exec_exits(124), "failed"),
        (_exec_raises(TimeoutError("exec timed out")), "timed_out"),
        (_exec_raises(_OutputLimitExceeded("output over 64 MiB")), "error"),
    ],
    ids=["success", "non-zero-exit", "fast-124-exit", "exec-timeout", "output-limit"],
)
def test_run_job_copies_the_outputs_before_it_removes_the_pod(lium, monkeypatch, exec_, status):
    events = []
    monkeypatch.setattr(lium, "exec", exec_)
    monkeypatch.setattr(lium, "download", lambda pod, remote, local: events.append(("download", remote)))
    monkeypatch.setattr(lium, "_request", lambda method, path, **kw: events.append((method, path)))

    result = lium.run_job(command="python train.py", gpu_type="H100", max_cost_usd=5, deadline_s=600,
                          outputs={"/root/train.log": "train.log"})

    assert result.status == status
    assert events == [("download", "/root/train.log"), ("DELETE", "/pods/pod-1")]
    assert result.outputs == {"/root/train.log": "train.log"} and result.cleanup == "removed"


def test_run_job_does_not_run_without_the_server_side_removal(lium, monkeypatch):
    def schedule(pod, termination_time):
        raise LiumError("503")

    monkeypatch.setattr(lium, "schedule_termination", schedule)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "error" and lium.calls["execs"] == []
    assert lium.calls["deletes"] == ["/pods/pod-1"]
    assert result.cleanup == "removed" and result.removal_scheduled_at is None


def test_run_job_failed_schedule_and_failed_delete_promise_no_cleanup(lium, monkeypatch):
    lium.calls["delete_fails"] = 99
    monkeypatch.setattr(lium, "schedule_termination", Mock(side_effect=LiumError("503")))

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert (r.status, r.cleanup, r.removal_scheduled_at, r.cost_usd) == ("error", "unknown", None, None)


def test_run_job_accepted_delete_of_a_pod_still_listed_is_not_removed(lium, monkeypatch):
    monkeypatch.setattr(lium, "ps", lambda: [_pod("x", status="DELETING")])

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert lium.calls["deletes"] == ["/pods/pod-1"]
    assert r.cleanup == "scheduled" and r.removal_scheduled_at is not None and r.cost_usd is None


def test_run_job_retries_a_refused_delete(lium):
    lium.calls["delete_fails"] = 2

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.cleanup == "removed" and lium.calls["deletes"] == ["/pods/pod-1"]


def test_run_job_delete_that_keeps_failing_leaves_the_scheduled_removal_and_estimates_to_it(lium):
    lium.calls["delete_fails"] = 99

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=3600)

    assert result.cleanup == "scheduled"
    # billed until the scheduled removal: about an hour at $2/h
    assert result.estimated_cost_usd == pytest.approx(2.0, abs=0.01)
    assert result.cost_usd is None  # the statement's 0.0421 is still accruing, not the final cost


def test_run_job_counts_a_slow_rent_response_in_the_budget_and_the_statement(lium, monkeypatch):
    clock = {"now": datetime(2026, 10, 15, 23, 59, 30, tzinfo=timezone.utc)}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"]

    rent = lium.rent

    def slow_rent(**kwargs):
        clock["now"] += timedelta(seconds=60)  # the response arrives a minute after the request, past midnight
        return rent(**kwargs)

    statements = []
    monkeypatch.setattr(bounded_job, "datetime", _Clock)
    monkeypatch.setattr(lium, "rent", slow_rent)
    monkeypatch.setattr(lium, "billing_statement", lambda **kw: statements.append(kw) or {"pods": []})

    # $1 at $2/h: 30 minutes from the request, not from the response
    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=1, deadline_s=7200)

    assert _scheduled_at(lium) == datetime(2026, 10, 16, 0, 29, 30, tzinfo=timezone.utc)
    assert r.estimated_cost_usd == pytest.approx(2.0 * 60 / 3600, abs=1e-6)
    assert statements[0]["start_day"] == "2026-10-15"


@pytest.mark.parametrize("phase", ["upload", "download"])
def test_run_job_stalled_transfer_is_cut_at_the_deadline_and_the_pod_removed(lium, monkeypatch, phase):
    stall = threading.Event()
    monkeypatch.setattr(lium, phase, lambda pod, **kw: stall.wait(30))

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=100, deadline_s=2, output_reserve_s=0.5,
                     inputs={"a": "/root/a"}, outputs={"/root/b": "b"})
    stall.set()

    assert r.seconds < 10 and lium.calls["deletes"] == ["/pods/pod-1"] and r.cleanup == "removed"
    if phase == "upload":
        assert r.status == "error" and "did not finish" in r.error and lium.calls["execs"] == []
    else:
        assert "/root/b" in r.missing_outputs


def test_run_job_no_time_left_after_boot_does_not_run(lium, monkeypatch):
    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=60, output_reserve_s=120)

    assert result.status == "timed_out" and lium.calls["execs"] == []
    assert result.cleanup == "removed"


@pytest.mark.parametrize("kwargs", [{"max_cost_usd": 0, "deadline_s": 60}, {"max_cost_usd": 1, "deadline_s": 0}])
def test_run_job_refuses_a_non_positive_limit(lium, kwargs):
    with pytest.raises(ValueError):
        lium.run_job(command="true", gpu_type="H100", **kwargs)
    assert "rent" not in lium.calls
