"""Background jobs: run_background() -> Job, re-attach by name, wait_for_port, logs, kill.

Every agent that served a model on a pod wrote its own `setsid nohup … &` plus a poll loop,
and at least one wasted minutes because its readiness check matched its own wrapper. The SDK
owns that now: a Job has a PID file, an exit-code file, a log, and a port probe that stops the
moment the process dies.
"""

import json
import os
import shlex
import shutil
import subprocess
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from lium.sdk import Config, ExecutorInfo, Job, Lium, LiumError, LiumNotFoundError, PodInfo
from lium.sdk.jobs import (
    build_job_launcher,
    build_port_probe,
    build_status_probe,
    identity_matches,
    job_paths,
    parse_status,
    process_identity,
    validate_job_name,
)


def _pod() -> PodInfo:
    return PodInfo(
        id="pod-1", name="serve", huid="swift-fox-c8", status="RUNNING", ssh_cmd="ssh root@1.2.3.4 -p 20299",
        ports={"22": 20299, "8000": 40123}, created_at="2026-01-01T00:00:00Z", updated_at="",
        executor=ExecutorInfo(
            id="exec-1", huid="brave-otter-11", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
            gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={}, specs={}, status="active",
            docker_in_docker=False, ip="1.2.3.4",
        ),
        template={"id": "tpl-1"}, removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )


class _Client(Lium):
    def __init__(self):
        super().__init__(Config(api_key="test"))

    def _request(self, method, endpoint, **kwargs):  # pragma: no cover - nothing here talks to the API
        raise AssertionError(f"unexpected API call {method} {endpoint}")


class _Stream:
    def __init__(self, text: str = "", exit_code: int = 0):
        self._text = text
        self.written = b""
        self.channel = SimpleNamespace(exit_status_ready=lambda: True, recv_exit_status=lambda: exit_code, close=lambda: None)

    def read(self):
        return self._text.encode()

    def write(self, data):
        self.written += data

    def close(self):
        pass


class _Sent(list):
    """The remote command lines sent, with ``.stdins[i]`` the stdin written for each."""

    stdins: list


def _ssh_answering(monkeypatch, client, answers):
    """Replace the SSH session with one that answers successive commands from ``answers``.

    Each answer is a string (stdout, exit 0), a ``(stdout, exit_code)`` tuple, or an
    exception instance to raise when connecting. The last answer repeats.
    """
    sent = _Sent()
    stdins: list[_Stream] = []  # what the client wrote to each session before closing it
    queue = list(answers)

    def _next():
        return queue.pop(0) if len(queue) > 1 else queue[0]

    class _Ssh:
        def exec_command(self, command, **kwargs):
            sent.append(command)
            answer = _next()
            stdout, code = answer if isinstance(answer, tuple) else (answer, 0)
            stdin = _Stream()
            stdins.append(stdin)
            return stdin, _Stream(stdout, code), _Stream("")

    @contextmanager
    def fake_connection(pod, timeout=30):
        if isinstance(queue[0], Exception):
            raise _next()
        yield _Ssh()

    monkeypatch.setattr(client, "ssh_connection", fake_connection)
    sent.stdins = stdins
    return sent


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# --- the remote command lines -----------------------------------------------------------------

def test_launcher_writes_pid_and_exit_files_and_detaches_from_the_session():
    line = build_job_launcher("vllm serve m --port 8000", name="vllm", job_dir="/workspace/logs")

    assert line.startswith("mkdir -p /workspace/logs || exit 1; ")
    assert "kill -0 \"$(cat /workspace/logs/vllm.pid)\"" in line and "exit 3" in line
    assert "rm -f /workspace/logs/vllm.exit /workspace/logs/vllm.id; " in line
    assert "printf %s 'vllm serve m --port 8000' > /workspace/logs/vllm.cmd; " in line
    assert "nohup setsid bash -c 'bash -lc '\"'\"'vllm serve m --port 8000'\"'\"'; echo $? > /workspace/logs/vllm.exit'" in line
    assert line.endswith(
        "> /workspace/logs/vllm.log 2>&1 < /dev/null & echo $! > /workspace/logs/vllm.pid; "
        + process_identity("$!")
        + " > /workspace/logs/vllm.id; echo $!"
    )


def test_launcher_refuses_a_live_name_only_when_the_pid_is_still_the_jobs_process():
    """The .pid file on /workspace survives a pod restart; the number may then belong to another
    process. "still running" therefore also needs the recorded identity to match."""
    line = build_job_launcher("vllm serve m --port 8000", name="vllm", job_dir="/workspace/logs")
    recorded = '"$(cat /workspace/logs/vllm.pid)"'

    assert (
        f"if [ -f /workspace/logs/vllm.pid ] && kill -0 {recorded} 2>/dev/null && "
        + identity_matches(recorded, "/workspace/logs/vllm.id")
        + "; then echo"
    ) in line


def test_process_identity_is_the_boot_id_and_the_start_time_from_proc_stat():
    # field 22 of /proc/<pid>/stat is the start time; the comm field can hold spaces, so the
    # line is cut after the closing parenthesis first (field 22 becomes field 20)
    assert process_identity("4242") == (
        "{ cat /proc/sys/kernel/random/boot_id 2>/dev/null; "
        "sed 's/.*) //' /proc/4242/stat 2>/dev/null | cut -d' ' -f20; } | tr '\\n' ' '"
    )
    # no .id file is a mismatch: the PID alone never decides
    assert identity_matches("4242", "/workspace/logs/j.id") == (
        '{ [ -f /workspace/logs/j.id ] && [ "$(' + process_identity("4242") + ')" = "$(cat /workspace/logs/j.id)" ]; }'
    )


# --- the identity check, run in a real bash ---------------------------------------------------
#
# Regression (arhangel66, lium#211 round 3): `identity_matches` used to read a missing `.id`
# file as "trust the PID", so a stale PID file from a job created before the `.id` file
# existed made status() say running, kill() signal a stranger and the launcher refuse a start.


def _bash(script: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, check=False)


@pytest.fixture
def live_process():
    """A process that is alive for the whole test and is nobody's job."""
    proc = subprocess.Popen(["sleep", "60"], start_new_session=True)  # its own group, like a job
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


def test_identity_check_fails_when_the_id_file_is_missing(tmp_path, live_process):
    id_file = str(tmp_path / "j.id")

    assert _bash(identity_matches(str(live_process.pid), id_file)).returncode != 0


@pytest.mark.skipif(not os.path.exists("/proc/self/stat"), reason="needs /proc (Linux)")
def test_identity_check_passes_for_the_recorded_process(tmp_path, live_process):
    id_file = str(tmp_path / "j.id")
    _bash(f"{process_identity(str(live_process.pid))} > {shlex.quote(id_file)}")

    assert _bash(identity_matches(str(live_process.pid), id_file)).returncode == 0


def test_status_probe_says_gone_for_a_live_pid_without_an_id_file(tmp_path, live_process):
    probe = build_status_probe(live_process.pid, str(tmp_path / "j.exit"), str(tmp_path / "j.id"))

    assert _bash(probe).stdout.strip() == "gone"
    assert live_process.poll() is None


def test_kill_refuses_a_live_pid_without_an_id_file(monkeypatch, tmp_path, live_process):
    """The command Job.kill() sends, run against a real process: nothing is signalled."""
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, [("", 1)])
    Job(client, _pod(), name="j", pid=live_process.pid, command="x", job_dir=str(tmp_path)).kill()

    result = _bash(sent[0])

    assert result.returncode != 0
    with pytest.raises(subprocess.TimeoutExpired):  # still alive: nothing was signalled
        live_process.wait(timeout=0.2)


def test_launcher_does_not_reclaim_a_live_pid_without_an_id_file(tmp_path, live_process):
    """A stale .pid from before the .id file existed does not block a new start under the name."""
    job_dir = tmp_path / "logs"
    job_dir.mkdir()
    (job_dir / "j.pid").write_text(f"{live_process.pid}\n")
    env = dict(os.environ)
    if shutil.which("setsid") is None:  # macOS has no setsid; the launcher's shape is what is under test
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "setsid").write_text('#!/bin/sh\nexec "$@"\n')
        (fake_bin / "setsid").chmod(0o755)
        env["PATH"] = f"{fake_bin}:{env['PATH']}"

    result = _bash(build_job_launcher("true", name="j", job_dir=str(job_dir)), env=env)

    assert result.returncode == 0, result.stderr
    new_pid = int(result.stdout.strip())
    assert new_pid != live_process.pid
    assert (job_dir / "j.pid").read_text().strip() == str(new_pid)
    assert live_process.poll() is None
    deadline = time.monotonic() + 10  # the new job (`true`) ends and its wrapper writes the exit code
    while not (job_dir / "j.exit").exists() and time.monotonic() < deadline:
        subprocess.run(["sleep", "0.05"], check=True)  # time.sleep is a no-op in this module
    assert (job_dir / "j.exit").read_text().strip() == "0"


def test_launcher_changes_directory_first_when_asked():
    line = build_job_launcher("python train.py", name="train", workdir="/workspace/repo")

    assert "cd /workspace/repo && python train.py" in line


def test_status_and_port_probes_are_bash_only():
    assert build_status_probe(4242, "/workspace/logs/j.exit") == (
        'if [ -f /workspace/logs/j.exit ]; then echo "exited $(cat /workspace/logs/j.exit)"; '
        "elif kill -0 4242 2>/dev/null; then echo running; else echo gone; fi"
    )
    # with the .id file, "running" also needs the live process to be the recorded one
    assert build_status_probe(4242, "/workspace/logs/j.exit", "/workspace/logs/j.id") == (
        'if [ -f /workspace/logs/j.exit ]; then echo "exited $(cat /workspace/logs/j.exit)"; '
        "elif kill -0 4242 2>/dev/null && " + identity_matches("4242", "/workspace/logs/j.id") + "; "
        "then echo running; else echo gone; fi"
    )
    assert build_port_probe(8000) == (
        "if timeout 3 bash -c 'exec 3<>/dev/tcp/127.0.0.1/8000' 2>/dev/null; then echo 'port open'; else echo 'port closed'; fi"
    )


def test_parse_status_reads_the_three_states():
    assert parse_status("exited 0\n") == {"state": "exited", "exit_code": 0}
    assert parse_status("exited 137\nport closed\n") == {"state": "exited", "exit_code": 137}
    assert parse_status("running\nport closed\n") == {"state": "running", "exit_code": None}
    assert parse_status("gone\n") == {"state": "gone", "exit_code": None}
    assert parse_status("") == {"state": "unknown", "exit_code": None}


@pytest.mark.parametrize("bad", ["", "../etc", "a b", "-lead", "x" * 65, "semi;colon"])
def test_job_names_are_file_name_stems(bad):
    with pytest.raises(ValueError, match="Invalid job name"):
        validate_job_name(bad)


def test_job_paths_sit_next_to_each_other():
    assert job_paths("vllm", "/workspace/logs/") == {
        "log_path": "/workspace/logs/vllm.log", "pid_file": "/workspace/logs/vllm.pid",
        "id_file": "/workspace/logs/vllm.id",
        "exit_file": "/workspace/logs/vllm.exit", "cmd_file": "/workspace/logs/vllm.cmd",
    }


# --- run_background ---------------------------------------------------------------------------

def test_run_background_returns_a_job_with_pid_and_paths(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["31337\n"])

    job = client.run_background(_pod(), "vllm serve m --port 8000", name="vllm")

    assert isinstance(job, Job)
    assert (job.name, job.pid, job.command) == ("vllm", 31337, "vllm serve m --port 8000")
    assert job.log_path == "/workspace/logs/vllm.log" and job.pid_file == "/workspace/logs/vllm.pid"
    assert "nohup setsid bash -c" in sent[0] and sent[0].endswith("echo $!")


def test_run_background_names_the_job_after_the_time_by_default(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["7\n"])

    job = client.run_background(_pod(), "sleep 1")

    assert job.name.startswith("job-") and job.name.endswith("Z")
    assert job.log_path == f"/workspace/logs/{job.name}.log"


def test_run_background_sends_env_over_stdin_and_applies_it_inside_the_job_shell(monkeypatch):
    """The value never sits in the pod's argv (DAH-2984): the exports travel over stdin
    as one variable, the job's `bash -lc` applies them after its profile (so the given
    value wins), and the .cmd file records the command as given, so job() returns the
    same command and no secret is written to disk."""
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["7\n"])

    job = client.run_background(_pod(), "run", name="j", env={"HF_HOME": "/workspace/hf"})

    assert sent[0].startswith('eval "$(cat)" && mkdir -p /workspace/logs || exit 1; ')
    assert "/workspace/hf" not in sent[0]
    assert sent.stdins[0].written == b"export LIUM_JOB_ENV='export HF_HOME=/workspace/hf'"
    inner = 'eval "$LIUM_JOB_ENV" || exit 1; unset LIUM_JOB_ENV; run'
    wrapper = f"bash -lc {shlex.quote(inner)}; echo $? > /workspace/logs/j.exit"
    assert f"bash -c {shlex.quote(wrapper)}" in sent[0]
    assert "printf %s run > /workspace/logs/j.cmd" in sent[0]
    assert job.command == "run"


def test_launcher_prelude_precedes_workdir_and_the_command_and_stays_out_of_the_cmd_file():
    line = build_job_launcher("python train.py", name="t", workdir="/workspace/repo", prelude="true; ")

    inner = "true; cd /workspace/repo && python train.py"
    wrapper = f"bash -lc {shlex.quote(inner)}; echo $? > /workspace/logs/t.exit"
    assert f"bash -c {shlex.quote(wrapper)}" in line
    assert "printf %s 'python train.py' > /workspace/logs/t.cmd" in line


def test_run_background_env_values_are_quoted_and_keys_validated(monkeypatch):
    """A value with quotes or `$(` reaches the job as one literal export; a key that is
    not a variable name is refused before anything is sent."""
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["7\n"])

    client.run_background(_pod(), "run", name="t", env={"MSG": 'say "hi" $(id)'})

    exports = "export MSG=" + shlex.quote('say "hi" $(id)')
    assert sent.stdins[0].written == b"export LIUM_JOB_ENV=" + shlex.quote(exports).encode()
    assert "$(id)" not in sent[0]

    for bad in ("BAD-NAME", "X; rm -rf /"):
        with pytest.raises(ValueError, match="Invalid environment variable name"):
            client.run_background(_pod(), "run", name="t", env={bad: "x"})
    with pytest.raises(ValueError, match="LIUM_JOB_ENV is reserved"):
        client.run_background(_pod(), "run", name="t", env={"LIUM_JOB_ENV": "x"})
    assert len(sent) == 1  # nothing else reached the pod


def test_run_background_refuses_a_name_whose_job_is_still_running(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, [("", 3)])

    with pytest.raises(LiumError, match="Could not start job vllm"):
        client.run_background(_pod(), "vllm serve m", name="vllm")


def test_run_background_fails_loudly_when_no_pid_came_back(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["", ])

    with pytest.raises(LiumError, match="launcher printed no PID"):
        client.run_background(_pod(), "run", name="j")


def test_run_background_rejects_a_bad_name_before_touching_the_pod(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["7\n"])

    with pytest.raises(ValueError):
        client.run_background(_pod(), "run", name="../escape")

    assert sent == []


def test_job_to_dict_is_json_serialisable():
    job = Job(_Client(), _pod(), name="vllm", pid=5, command="vllm serve m")

    data = json.loads(json.dumps(job.to_dict()))

    assert data["name"] == "vllm" and data["pid"] == 5 and data["pod_id"] == "pod-1"
    assert data["log_path"] == "/workspace/logs/vllm.log" and data["exit_file"] == "/workspace/logs/vllm.exit"
    assert data["id_file"] == "/workspace/logs/vllm.id"


# --- re-attaching -----------------------------------------------------------------------------

def test_job_reattaches_by_name_from_the_pid_and_cmd_files(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["4242\n\n---cmd---vllm serve m --port 8000"])

    job = client.job(_pod(), "vllm")

    assert (job.pid, job.command, job.log_path) == (4242, "vllm serve m --port 8000", "/workspace/logs/vllm.log")
    assert "cat /workspace/logs/vllm.pid" in sent[0] and "cat /workspace/logs/vllm.cmd" in sent[0]


def test_job_reattach_reports_a_missing_job(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, [("---cmd---", 1)])

    with pytest.raises(LiumNotFoundError, match="No job named vllm"):
        client.job(_pod(), "vllm")


def test_jobs_lists_every_pid_file(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["vllm 4242\ntrain 99\nnot a job line\n"])

    jobs = client.jobs(_pod())

    assert [(j.name, j.pid) for j in jobs] == [("vllm", 4242), ("train", 99)]


# --- status, poll, wait -----------------------------------------------------------------------

def test_status_poll_and_is_running(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n", "exited 0\n"])
    job = Job(client, _pod(), name="j", pid=1, command="x")

    assert job.is_running() is True
    assert job.poll() == 0


def test_poll_raises_when_the_process_is_gone_without_an_exit_code(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["gone\n"])

    with pytest.raises(LiumError, match="gone without an exit code"):
        Job(client, _pod(), name="j", pid=1, command="x").poll()


def test_wait_returns_the_exit_code_when_the_job_ends(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n", "running\n", "exited 2\n"])

    assert Job(client, _pod(), name="j", pid=1, command="x").wait(timeout=100) == 2


def test_wait_times_out_and_names_the_log(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\n"])
    clock = iter([0.0, 0.0, 1000.0, 1000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="/workspace/logs/j.log"):
        Job(client, _pod(), name="j", pid=1, command="x").wait(timeout=10)


# --- wait_for_port ----------------------------------------------------------------------------

def test_wait_for_port_returns_once_the_port_answers(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["running\nport closed\n", "running\nport closed\n", "running\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=600)

    assert len(sent) == 3
    assert "kill -0 1" in sent[0] and "/dev/tcp/127.0.0.1/8000" in sent[0]


def test_wait_for_port_fails_at_once_when_the_job_died_and_shows_the_log(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 1\nport closed\n", "Traceback: CUDA out of memory\n"])

    with pytest.raises(LiumError, match="exited with code 1 before port 8000") as info:
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=600)

    assert "CUDA out of memory" in str(info.value)


def test_wait_for_port_does_not_take_a_port_held_by_someone_else_for_a_dead_job(monkeypatch):
    """The job died; another process answers on the port. That is not a ready server."""
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 1\nport open\n", "boom\n"])

    with pytest.raises(LiumError, match="exited with code 1 before port 8000.*not by this job"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_accepts_a_launcher_that_forked_its_server_and_exited_cleanly(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["exited 0\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_fails_when_the_process_vanished(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["gone\nport closed\n", ""])

    with pytest.raises(LiumError, match="is gone before port 8000"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_times_out_while_the_job_still_runs(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["running\nport closed\n"])
    clock = iter([0.0, 5.0, 10_000.0, 10_000.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="did not answer within 60s.*job vllm is running"):
        Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000, timeout=60)


def test_wait_for_port_keeps_polling_through_a_dropped_ssh_connection(monkeypatch):
    import paramiko

    client = _Client()
    _ssh_answering(monkeypatch, client, [paramiko.SSHException("banner"), "running\nport open\n"])

    Job(client, _pod(), name="vllm", pid=1, command="x").wait_for_port(8000)


def test_wait_for_port_probes_another_host_when_asked(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["running\nport open\n"])

    Job(client, _pod(), name="j", pid=1, command="x").wait_for_port(9000, host="0.0.0.0")

    assert "/dev/tcp/0.0.0.0/9000" in sent[0]


def test_client_wait_for_port_without_a_job(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["port closed\n", "port open\n"])

    client.wait_for_port(_pod(), 8000)

    assert len(sent) == 2 and "kill -0" not in sent[0]


def test_client_wait_for_port_times_out(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, ["port closed\n"])
    clock = iter([0.0, 1.0, 999.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="Port 8000 on pod serve"):
        client.wait_for_port(_pod(), 8000, timeout=5)


# --- wait_ready(ready_port=) ------------------------------------------------------------------

def test_wait_ready_with_ready_port_waits_for_running_then_for_the_port(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "ps", lambda: [_pod()])
    waited = []
    monkeypatch.setattr(client, "wait_for_port", lambda pod, port, **kw: waited.append((pod.id, port, round(kw["timeout"]))))

    pod = client.wait_ready("pod-1", timeout=300, ready_port=8000)

    assert pod.id == "pod-1" and waited == [("pod-1", 8000, 300)]


def test_wait_ready_with_ready_port_returns_none_when_the_port_never_answers(monkeypatch):
    client = _Client()
    monkeypatch.setattr(client, "ps", lambda: [_pod()])

    def never(pod, port, **kw):
        raise TimeoutError("no")

    monkeypatch.setattr(client, "wait_for_port", never)

    assert client.wait_ready("pod-1", timeout=300, ready_port=8000) is None


# --- logs and kill ----------------------------------------------------------------------------

def test_logs_reads_the_whole_file_or_a_tail(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["line1\nline2\n"])
    job = Job(client, _pod(), name="j", pid=1, command="x")

    assert job.logs() == "line1\nline2\n"
    assert job.logs(tail=40) == "line1\nline2\n"
    assert sent[0].startswith("cat /workspace/logs/j.log") and sent[1].startswith("tail -n 40 /workspace/logs/j.log")


def test_kill_signals_the_process_group(monkeypatch):
    client = _Client()
    sent = _ssh_answering(monkeypatch, client, ["", ])
    job = Job(client, _pod(), name="j", pid=4242, command="x")

    assert job.kill() is True
    assert job.kill("SIGKILL") is True
    assert "kill -TERM -- -4242 2>/dev/null || kill -TERM 4242" in sent[0]
    assert "kill -KILL -- -4242 2>/dev/null || kill -KILL 4242" in sent[1]
    # the signal is sent only when the PID is still the job's own process
    assert sent[0].startswith(identity_matches("4242", "/workspace/logs/j.id") + " && { kill -TERM")


def test_kill_reports_false_when_the_pid_is_no_longer_the_job(monkeypatch):
    client = _Client()
    _ssh_answering(monkeypatch, client, [("", 1)])  # the identity check failed: nothing was signalled
    job = Job(client, _pod(), name="j", pid=4242, command="x")

    assert job.kill() is False


def test_kill_rejects_an_unknown_signal_shape():
    with pytest.raises(ValueError):
        Job(_Client(), _pod(), name="j", pid=1, command="x").kill("TERM; rm -rf /")
