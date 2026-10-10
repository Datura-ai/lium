"""`Lium.run_job`: one bounded job that always ends with the pod removed and a terminal status."""

from datetime import datetime, timezone

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


def _pod(name):
    return PodInfo(
        id="pod-1", name=name, status="RUNNING", huid="pod-huid", ssh_cmd="ssh root@1.2.3.4 -p 22",
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
    monkeypatch.setattr(lium, "exec", lambda pod, command, env=None, timeout=None: {"exit_code": 124, "stdout": "", "stderr": ""})
    before = datetime.now(timezone.utc)

    # $1 at $2/h lasts 30 minutes, before the 2-hour deadline
    result = lium.run_job(command="sleep 9999", gpu_type="H100", max_cost_usd=1, deadline_s=7200)

    assert abs((_scheduled_at(lium) - before).total_seconds() - 1800) < 5
    assert result.status == "budget_exhausted" and result.cleanup == "removed"


def test_run_job_reports_timed_out_when_the_deadline_stops_the_command(lium, monkeypatch):
    monkeypatch.setattr(lium, "exec", lambda pod, command, env=None, timeout=None: {"exit_code": 137, "stdout": "", "stderr": ""})

    result = lium.run_job(command="sleep 9999", gpu_type="H100", max_cost_usd=100, deadline_s=600)

    assert result.status == "timed_out" and result.cleanup == "removed"


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
    monkeypatch.setattr(lium, "ps", lambda: [_pod(lium.calls["rent"]["name"]), _pod("someone-elses-pod")])

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "rent_failed" and "read timed out" in result.error
    assert lium.calls["deletes"] == ["/pods/pod-1"] and result.cleanup == "removed"


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


def test_run_job_does_not_run_without_the_server_side_removal(lium, monkeypatch):
    def schedule(pod, termination_time):
        raise LiumError("503")

    monkeypatch.setattr(lium, "schedule_termination", schedule)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "error" and lium.calls["execs"] == []
    assert lium.calls["deletes"] == ["/pods/pod-1"]


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


def test_run_job_no_time_left_after_boot_does_not_run(lium, monkeypatch):
    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=60, output_reserve_s=120)

    assert result.status == "timed_out" and lium.calls["execs"] == []
    assert result.cleanup == "removed"


@pytest.mark.parametrize("kwargs", [{"max_cost_usd": 0, "deadline_s": 60}, {"max_cost_usd": 1, "deadline_s": 0}])
def test_run_job_refuses_a_non_positive_limit(lium, kwargs):
    with pytest.raises(ValueError):
        lium.run_job(command="true", gpu_type="H100", **kwargs)
    assert "rent" not in lium.calls
