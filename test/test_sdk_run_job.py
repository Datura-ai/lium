"""`Lium.run_job`: one bounded job that always ends with the pod removed and a terminal status."""

import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import requests

from lium.sdk import Config, Lium, LiumError, PodStartError, RentResult
from lium.sdk import bounded_job
from lium.sdk.exceptions import ClusterNotListedError, LiumServerError
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


def _write(local):
    with open(local, "w") as f:
        f.write("out")


@pytest.fixture
def lium(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # downloads land in the working directory
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
    monkeypatch.setattr(client, "upload", lambda pod, local, remote, **_: calls["uploads"].append((local, remote)))
    monkeypatch.setattr(client, "download", lambda pod, remote, local, **kw: _write(local) or calls["downloads"].append((remote, local, kw)))
    monkeypatch.setattr(client, "exec", lambda pod, command, env=None, timeout=None, **_: (
        calls["execs"].append((command, timeout)) or {"exit_code": 0, "stdout": "done\n", "stderr": "", "success": True}
    ))
    monkeypatch.setattr(client, "billing_statement", lambda **kw: {"pods": [{"pod_id": "pod-1", "total": 0.0421}]})
    monkeypatch.setattr(client, "ps", lambda: [])
    monkeypatch.setattr(bounded_job.time, "sleep", lambda s: None)
    return client


def _scheduled_at(lium):
    # set before boot, and again once booted (the server's termination_hours replaces it then)
    first, again = lium.calls["schedules"]
    assert first == again
    pod_id, when = first
    assert pod_id == "pod-1"
    return datetime.fromisoformat(when.replace("Z", "+00:00"))


@pytest.mark.parametrize("deadline_s, hours", [(600, 1), (3601, 2), (10**7, 720)])
def test_run_job_sends_a_server_side_removal_with_the_rent(lium, deadline_s, hours):
    lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=deadline_s)
    assert lium.calls["rent"]["termination_hours"] == hours


def test_rent_by_spec_and_up_send_termination_hours(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(client, "get_executor", lambda executor_id: Mock(id=executor_id))
    sent = []
    monkeypatch.setattr(client, "supports", lambda feature: True)
    monkeypatch.setattr(client, "_ensure_ssh_keys_registered", lambda *a, **k: None)
    monkeypatch.setattr(client, "_pod_ids_before_rent", lambda: frozenset())

    def request(method, path, json=None, **kwargs):
        sent.append(json)
        raise LiumError("stop here")

    monkeypatch.setattr(client, "_request", request)
    with pytest.raises(LiumError):
        client.rent(gpu_type="H100", ssh_keys=["ssh-ed25519 AAAA k"], termination_hours=3)
    with pytest.raises(LiumError):
        client.up(executor_id="e1", template_id="t1", ssh_keys=["ssh-ed25519 AAAA k"], termination_hours=4)
    assert [body["termination_hours"] for body in sent] == [3, 4]


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
    assert "timeout --verbose -k 10 " in command and "python train.py --epochs 1'" in command
    assert int(command.split("-k 10 ")[1].split()[0]) <= 1800 - 120 and timeout <= 1800 - 120 + 30
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


def _exec_locally(pod, command, env=None, timeout=None, **_):
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
    assert result.stderr == ""  # the pod's timeout marker is not handed to the caller


def test_run_job_slow_ssh_does_not_turn_a_fast_124_exit_into_a_timeout(lium, monkeypatch):
    real, offset = bounded_job.time.monotonic, [0.0]
    monkeypatch.setattr(bounded_job.time, "monotonic", lambda: real() + offset[0])

    def slow_ssh_exec(pod, command, env=None, timeout=None, **_):
        offset[0] += 5  # connecting took longer than the whole command limit
        return _exec_locally(pod, command, env, timeout)

    monkeypatch.setattr(lium, "exec", slow_ssh_exec)

    result = lium.run_job(command="exit 124", gpu_type="H100", max_cost_usd=100, deadline_s=3, output_reserve_s=1)

    assert (result.status, result.exit_code) == ("failed", 124)


def test_run_job_failed_command_still_copies_outputs_and_names_the_missing_one(lium, monkeypatch):
    monkeypatch.setattr(lium, "exec", lambda pod, command, env=None, timeout=None, **_: {"exit_code": 2, "stdout": "", "stderr": "boom"})

    def download(pod, remote, local, **_):
        if remote == "/root/model.pt":
            raise FileNotFoundError("no such file")
        _write(local)

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


def _with_replay_key(exc):
    exc.idempotency_key = "same-rental-key"
    return exc


@pytest.mark.parametrize(
    "exc, cleanup",
    [
        (LiumError("no node matches"), "not_rented"),
        (requests.ReadTimeout("response lost"), "unknown"),
        (requests.ConnectionError("connection reset"), "unknown"),
        (LiumServerError("502"), "unknown"),
        (LiumError("rent outcome unknown", code="rent_outcome_unknown"), "unknown"),
        (LiumError("still in progress", code="idempotency_in_progress"), "unknown"),
        (_with_replay_key(LiumError("rent outcome unknown; a pod may exist")), "unknown"),
        (ClusterNotListedError("not listed", confirmed=False), "unknown"),
    ],
    ids=["refused", "read-timeout", "connection-lost", "server-error", "unknown-code", "in-progress-code",
         "replay-key", "cluster-not-listed"],
)
def test_run_job_rent_failure_with_no_pod_listed(lium, monkeypatch, exc, cleanup):
    monkeypatch.setattr(lium, "rent", Mock(side_effect=exc))

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    # an unanswered rent can still create the pod after the listing, so only a refusal is not_rented
    assert result.status == "rent_failed" and result.cleanup == cleanup and result.pod_id is None
    assert lium.calls["deletes"] == [] and result.cost_usd is None
    assert (result.name in result.error) == (cleanup == "unknown")


def test_run_job_boot_failure_removes_the_pod(lium, monkeypatch):
    def wait_ready(pod, timeout):
        raise PodStartError("pod reached FAILED", pod_id="pod-1", status="FAILED")

    monkeypatch.setattr(lium, "wait_ready", wait_ready)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "boot_failed" and lium.calls["execs"] == []
    assert lium.calls["deletes"] == ["/pods/pod-1"]


def test_run_job_ssh_error_reports_error_and_removes_the_pod(lium, monkeypatch):
    def exec_(pod, command, env=None, timeout=None, **_):
        raise OSError("connection reset")

    monkeypatch.setattr(lium, "exec", exec_)

    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert result.status == "error" and "connection reset" in result.error
    assert result.cleanup == "removed"


def _exec_exits(code):
    return lambda pod, command, env=None, timeout=None, **_: {"exit_code": code, "stdout": "", "stderr": ""}


def _exec_raises(exc):
    def exec_(pod, command, env=None, timeout=None, **_):
        raise exc

    return exec_


@pytest.mark.parametrize(
    "exec_, status",
    [
        (_exec_exits(0), "succeeded"),
        (_exec_exits(2), "failed"),
        (_exec_exits(124), "failed"),
        (_exec_raises(TimeoutError("exec timed out")), "timed_out"),
        (_exec_raises(LiumError("SSH connection dropped")), "error"),
    ],
    ids=["success", "non-zero-exit", "fast-124-exit", "exec-timeout", "ssh-error"],
)
def test_run_job_copies_the_outputs_before_it_removes_the_pod(lium, monkeypatch, exec_, status):
    events = []
    monkeypatch.setattr(lium, "exec", exec_)
    monkeypatch.setattr(lium, "download", lambda pod, remote, local, **_: _write(local) or events.append(("download", remote)))
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
    monkeypatch.setattr(bounded_job, "_CONFIRM_S", 0.2)  # sleeps are no-ops here: the wait is a real-clock spin

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert lium.calls["deletes"] == ["/pods/pod-1"]
    assert r.cleanup == "scheduled" and r.removal_scheduled_at is not None and r.cost_usd is None


def test_run_job_cleanup_wait_is_bounded_by_wall_clock_on_a_slow_api(lium, monkeypatch):
    elapsed = [0.0]
    monkeypatch.setattr(bounded_job.time, "monotonic", lambda: elapsed[0])

    def slow_ps():
        elapsed[0] += 30
        return [_pod("x", status="DELETING")]

    monkeypatch.setattr(lium, "ps", slow_ps)

    assert bounded_job._remove(lium, "pod-1") is False
    assert elapsed[0] <= bounded_job._CONFIRM_S


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


def test_run_job_no_time_left_after_boot_does_not_run(lium, monkeypatch):
    result = lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=60, output_reserve_s=120)

    assert result.status == "timed_out" and lium.calls["execs"] == []
    assert result.cleanup == "removed"


@pytest.mark.parametrize("kwargs", [{"max_cost_usd": 0, "deadline_s": 60}, {"max_cost_usd": 1, "deadline_s": 0}])
def test_run_job_refuses_a_non_positive_limit(lium, kwargs):
    with pytest.raises(ValueError):
        lium.run_job(command="true", gpu_type="H100", **kwargs)
    assert "rent" not in lium.calls


def test_run_job_download_that_fails_midway_never_writes_the_callers_path(lium, monkeypatch, tmp_path):
    def broken_download(pod, remote, local, **_):
        _write(local)  # a partial file, then the host goes away
        raise TimeoutError("download of /root/ckpt.pt did not finish within 2s")

    monkeypatch.setattr(lium, "download", broken_download)

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=100, deadline_s=600,
                     outputs={"/root/ckpt.pt": "ckpt.pt"})

    assert "/root/ckpt.pt" in r.missing_outputs and r.outputs == {}
    assert sorted(p.name for p in tmp_path.iterdir()) == []  # neither ckpt.pt nor a partial side file


def test_run_job_bounds_every_transfer_and_the_captured_output(lium, monkeypatch):
    seen = {}
    monkeypatch.setattr(lium, "upload", lambda pod, local, remote, **kw: seen.setdefault("upload", kw))
    monkeypatch.setattr(lium, "exec", lambda pod, command, env=None, timeout=None, **kw: (
        seen.setdefault("exec", kw) and None) or {"exit_code": 0, "stdout": "tail", "stderr": "", "truncated": True})

    r = lium.run_job(command="true", gpu_type="H100", max_cost_usd=100, deadline_s=600, inputs={"a": "/root/a"},
                     outputs={"/root/b": "b"}, output_tail_bytes=1000, max_download_bytes=5000)

    assert 0 < seen["upload"]["timeout"] <= 600 and seen["exec"] == {"output_tail_bytes": 1000}
    (_, _, kw), = lium.calls["downloads"]
    assert kw["max_bytes"] == 5000 and 0 < kw["timeout"] <= 600
    assert r.status == "succeeded" and "cut to their last 1000 bytes" in r.error


def test_run_job_interrupted_during_the_rent_removes_the_pod_it_created_and_re_raises(lium, monkeypatch):
    def rent(**kwargs):
        lium.calls["rent"] = kwargs
        raise KeyboardInterrupt  # the server created the pod; Ctrl-C landed before the answer

    monkeypatch.setattr(lium, "rent", rent)
    monkeypatch.setattr(lium, "ps", lambda: [p for p in [_pod(lium.calls["rent"]["name"])]
                                             if f"/pods/{p.id}" not in lium.calls["deletes"]])

    with pytest.raises(KeyboardInterrupt):
        lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert lium.calls["deletes"] == ["/pods/pod-1"]


def test_run_job_interrupted_after_the_rent_still_removes_the_pod(lium, monkeypatch):
    def interrupted(pod, termination_time):
        raise KeyboardInterrupt

    monkeypatch.setattr(lium, "schedule_termination", interrupted)

    with pytest.raises(KeyboardInterrupt):
        lium.run_job(command="true", gpu_type="H100", max_cost_usd=5, deadline_s=600)

    assert lium.calls["deletes"] == ["/pods/pod-1"]


class _StallingClient:
    """An SSH client whose remote file never answers (or answers forever) until the client is closed."""

    def __init__(self, endless=False):
        self.closed, self.endless, self.read = threading.Event(), endless, 0

    def close(self):
        self.closed.set()

    def open_sftp(self):
        client = self

        class File:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n):
                if client.endless:
                    client.read += n
                    return b"x" * n
                if client.closed.wait(30):
                    raise EOFError("connection closed")
                return b""

        return Mock(open=lambda remote, mode: File(), close=lambda: None, stat=lambda remote: Mock(st_mode=0o100644))


def _ssh_with(monkeypatch, lium, client):
    from contextlib import contextmanager

    @contextmanager
    def ssh_connection(pod, timeout=30, deadline=None):
        try:
            yield client
        finally:
            client.close()

    monkeypatch.setattr(lium, "ssh_connection", ssh_connection)


def test_download_from_a_stalling_host_is_cut_at_the_timeout_and_its_connection_closed(lium, monkeypatch, tmp_path):
    del lium.download  # the real SDK transfer, not the fixture's stand-in
    threads = threading.active_count()
    for _ in range(3):  # repeated stalls leave no transfer behind
        client = _StallingClient()
        _ssh_with(monkeypatch, lium, client)
        started = datetime.now()
        with pytest.raises(TimeoutError):
            lium.download(_pod("p"), remote="/root/b", local=str(tmp_path / "b"), timeout=0.3)
        assert (datetime.now() - started).total_seconds() < 5 and client.closed.is_set()
    assert threading.active_count() == threads


def test_download_past_the_byte_limit_stops_reading(lium, monkeypatch, tmp_path):
    del lium.download
    client = _StallingClient(endless=True)
    _ssh_with(monkeypatch, lium, client)

    with pytest.raises(ValueError, match="limit"):
        lium.download(_pod("p"), remote="/dev/zero", local=str(tmp_path / "b"), max_bytes=100_000, timeout=30)

    assert client.read < 200_000 and not (tmp_path / "b").exists()  # the partial file is not left behind


def test_transfer_deadline_covers_the_ssh_handshake(lium, monkeypatch, tmp_path):
    del lium.download
    asked = []

    def ssh_connection(pod, timeout=30, deadline=None):
        asked.append(deadline - time.monotonic())
        raise TimeoutError("auth stalled")

    monkeypatch.setattr(lium, "ssh_connection", ssh_connection)
    with pytest.raises(TimeoutError):
        lium.download(_pod("p"), remote="/root/b", local=str(tmp_path / "b"), timeout=6)
    # the connect gets the transfer's own deadline, not a separate budget of its own
    assert len(asked) == 1 and 5 < asked[0] <= 6


def test_download_with_only_a_timeout_copies_the_file(lium, monkeypatch, tmp_path):
    del lium.download
    data = [b"a" * 40000, b"b" * 10]

    class File:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n):
            return data.pop(0) if data else b""

    client = Mock(open_sftp=lambda: Mock(open=lambda remote, mode: File(), stat=lambda remote: Mock(st_mode=0o100644)))
    _ssh_with(monkeypatch, lium, client)
    lium.download(_pod("p"), remote="/root/result", local=str(tmp_path / "result"), timeout=60)
    assert (tmp_path / "result").read_bytes() == b"a" * 40000 + b"b" * 10


def _stubborn_ssh(monkeypatch, attempts):
    """An SSHClient whose connect makes auth attempt after auth attempt, each 0.2 s, until closed."""
    from lium.sdk import client as sdk_client

    class Client:
        def __init__(self):
            self.closed = threading.Event()

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            attempts.append(kwargs)
            for _ in range(100):
                if self.closed.wait(0.2):
                    raise EOFError("closed")

        def close(self):
            self.closed.set()

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", Client)
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")


def test_ssh_connect_with_a_deadline_ends_by_it_whatever_auth_retries(monkeypatch, tmp_path):
    attempts = []
    _stubborn_ssh(monkeypatch, attempts)
    key = tmp_path / "id"
    key.write_text("not a key")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with Lium(Config(api_key="k", ssh_key_path=key)).ssh_connection(_pod("p"), deadline=started + 0.5):
            pass
    assert time.monotonic() - started < 1.5
    assert attempts[0]["timeout"] <= 0.5 and attempts[0]["auth_timeout"] <= 0.5  # no wait past the deadline


def test_ssh_connect_past_its_deadline_fails_without_connecting(monkeypatch, tmp_path):
    attempts = []
    _stubborn_ssh(monkeypatch, attempts)
    key = tmp_path / "id"
    key.write_text("not a key")
    with pytest.raises(TimeoutError):
        with Lium(Config(api_key="k", ssh_key_path=key)).ssh_connection(_pod("p"), deadline=time.monotonic()):
            pass
    assert attempts == []


def test_ssh_connect_bounds_banner_and_auth_by_the_connect_timeout(monkeypatch, tmp_path):
    from lium.sdk import client as sdk_client

    seen = {}

    class Client(sdk_client.paramiko.SSHClient):
        def connect(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(sdk_client.paramiko, "SSHClient", Client)
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    key = tmp_path / "id"
    key.write_text("not a key")
    with Lium(Config(api_key="k", ssh_key_path=key)).ssh_connection(_pod("p"), timeout=2):
        pass
    assert (seen["timeout"], seen["banner_timeout"], seen["auth_timeout"]) == (2, 2, 2)


class _Channel:
    """An exec channel that sends ``chunks`` (then, if ``endless``, never stops) before exiting 0."""

    def __init__(self, chunks, endless=False):
        self.chunks, self.endless, self.closed = list(chunks), endless, False

    def recv_ready(self):
        return bool(self.chunks) or self.endless

    def recv(self, n):
        return self.chunks.pop(0) if self.chunks else b"y" * n

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, n):
        return b""

    def exit_status_ready(self):
        return True

    eof_received = True

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True


def _exec_over(monkeypatch, lium, channel):
    stdout = Mock(channel=channel)
    client = Mock(exec_command=lambda command: (Mock(), stdout, Mock()))
    monkeypatch.setenv("LIUM_SSH_REUSE", "0")  # the stand-in is a per-call connection, not a kept one
    _ssh_with(monkeypatch, lium, Mock(wraps=client, exec_command=client.exec_command, close=lambda: None))


def test_exec_with_an_output_limit_keeps_the_tail(lium, monkeypatch):
    del lium.exec
    _exec_over(monkeypatch, lium, _Channel([b"a" * 70_000, b"b" * 70_000, b"END\n"]))

    out = lium.exec(_pod("p"), command="noisy", timeout=30, output_tail_bytes=1000)

    assert out["truncated"] and len(out["stdout"]) == 1000 and out["stdout"].endswith("bEND\n")


def test_exec_with_a_zero_tail_keeps_no_output(lium, monkeypatch):
    del lium.exec
    _exec_over(monkeypatch, lium, _Channel([b"a" * 70_000, b"END\n"]))

    out = lium.exec(_pod("p"), command="noisy", timeout=30, output_tail_bytes=0)

    assert out["truncated"] and out["stdout"] == ""


def test_exec_refuses_a_negative_tail_before_connecting(lium, monkeypatch):
    del lium.exec
    monkeypatch.setattr(lium, "ssh_connection", Mock(side_effect=AssertionError("connected")))
    with pytest.raises(ValueError, match="output_tail_bytes"):
        lium.exec(_pod("p"), command="true", output_tail_bytes=-1)


@pytest.mark.parametrize("kwargs", [{"output_tail_bytes": -1}, {"max_download_bytes": -1}])
def test_run_job_refuses_a_negative_size_limit_before_renting(lium, kwargs):
    with pytest.raises(ValueError):
        lium.run_job(command="true", gpu_type="H100", max_cost_usd=1, deadline_s=60, **kwargs)
    assert "rent" not in lium.calls


@pytest.mark.parametrize("tail", [0, 5])
def test_run_job_timeout_is_classified_whatever_the_output_tail(lium, monkeypatch, tail):
    def capped(pod, command, output_tail_bytes, **kw):
        run = _exec_locally(pod, command, **kw)
        run["stderr"] = run["stderr"].encode()[-output_tail_bytes:].decode() if output_tail_bytes else ""
        return run

    monkeypatch.setattr(lium, "exec", capped)

    r = lium.run_job(command="echo started >&2; sleep 30", gpu_type="H100", max_cost_usd=100, deadline_s=3,
                     output_reserve_s=1, output_tail_bytes=tail)

    assert (r.status, r.exit_code) == ("timed_out", 124) and len(r.stderr.encode()) <= tail


def test_exec_with_an_output_limit_cuts_a_host_that_never_stops_sending(lium, monkeypatch):
    del lium.exec
    channel = _Channel([], endless=True)
    _exec_over(monkeypatch, lium, channel)

    with pytest.raises(TimeoutError):
        lium.exec(_pod("p"), command="true", timeout=0.3, output_tail_bytes=1000)

    assert channel.closed


def test_run_job_timeout_marker_needs_the_signal_not_elapsed_time(lium, monkeypatch, tmp_path):
    # a `date` that jumps an hour between calls stands in for the wrapper being descheduled or the VM paused
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "date").write_text(f"#!/bin/sh\nn=$(cat {fake}/n 2>/dev/null || echo 0); "
                               f"echo $((n + 3600000000000)) > {fake}/n; echo $n\n")
    (fake / "date").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    monkeypatch.setattr(lium, "exec", _exec_locally)

    r = lium.run_job(command="echo oops >&2; exit 124", gpu_type="H100", max_cost_usd=100, deadline_s=600,
                     output_reserve_s=1)

    assert (r.status, r.exit_code, r.stderr) == ("failed", 124, "oops\n")
