"""SDK ergonomics for programs: rent-and-clean-up, wait, timeouts, detach, GPU stats, to_dict.

A script that rents a pod has to write the same twenty lines every time: poll
`ps` until RUNNING, remember to `rm` in a `finally`, wrap long commands in
`nohup setsid ... < /dev/null &`, parse `nvidia-smi`. These belong in the SDK.
"""

import inspect
import json
import re
import time
from types import SimpleNamespace

import pytest

from lium.sdk import Config, ExecutorInfo, Lium, LiumError, PodInfo, Template
from lium.sdk.models import GpuStats


def _pod(status: str = "RUNNING", ssh_cmd: str | None = "ssh root@1.2.3.4 -p 20299") -> PodInfo:
    return PodInfo(
        id="pod-1", name="job", huid="swift-fox-c8", status=status, ssh_cmd=ssh_cmd,
        ports={"22": 20299}, created_at="2026-01-01T00:00:00Z", updated_at="",
        executor=_executor(), template={"id": "tpl-1"}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


def _executor() -> ExecutorInfo:
    return ExecutorInfo(
        id="exec-1", huid="brave-otter-11", machine_name="NVIDIA H100 80GB HBM3", gpu_type="H100",
        gpu_count=1, price_per_hour=2.0, price_per_gpu=2.0, location={"country": "US"},
        specs={"gpu": {"driver": "550.0", "details": [{"name": "H100 80GB HBM3"}]}},
        status="active", docker_in_docker=False, ip="1.2.3.4",
    )


class _Client(Lium):
    """A client whose rent, listing and removal are recorded rather than sent."""

    def __init__(self, ps_sequence=None):
        super().__init__(Config(api_key="test"))
        self.calls: list = []
        self._ps_sequence = list(ps_sequence or [])

    def _rent(self, **kwargs):
        # a fake that took any keyword set hid a rental() that no longer matched the real
        # _rent() (main's `image`, lium#161): every call is bound against the real signature
        inspect.signature(Lium._rent).bind(self, **kwargs)
        self.calls.append(("rent", kwargs))
        return {"id": "pod-1", "name": kwargs.get("name")}

    def ps(self):
        self.calls.append(("ps",))
        if len(self._ps_sequence) > 1:
            return self._ps_sequence.pop(0)
        return self._ps_sequence[0] if self._ps_sequence else []

    def _request(self, method, endpoint, **kwargs):
        self.calls.append((method, endpoint))
        return SimpleNamespace(json=lambda: {})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


# --- up(wait=True), pod_by_name, rental() -----------------------------------------------------

def test_up_without_wait_returns_the_rent_payload_unchanged():
    client = _Client()

    created = client.up(executor_id="exec-1", name="job")

    assert created == {"id": "pod-1", "name": "job"}


def test_up_with_wait_returns_the_ready_pod():
    client = _Client(ps_sequence=[[_pod("PENDING", None)], [_pod("RUNNING")]])

    pod = client.up(executor_id="exec-1", name="job", wait=True)

    assert isinstance(pod, PodInfo)
    assert pod.status == "RUNNING" and pod.ssh_cmd


def test_up_with_wait_names_the_billing_pod_when_it_never_becomes_ready(monkeypatch):
    client = _Client(ps_sequence=[[_pod("PENDING", None)]])
    clock = iter([0, 0, 1000, 1000, 1000])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(LiumError, match="pod-1"):
        client.up(executor_id="exec-1", name="job", wait=True, timeout=5)


def test_pod_by_name_matches_name_huid_or_id():
    client = _Client(ps_sequence=[[_pod()]])

    assert client.pod_by_name("job").id == "pod-1"
    assert client.pod_by_name("swift-fox-c8").id == "pod-1"
    assert client.pod_by_name("pod-1").id == "pod-1"
    assert client.pod_by_name("nope") is None


def test_rental_removes_the_pod_after_the_block():
    client = _Client(ps_sequence=[[_pod()]])

    with client.rental(executor_id="exec-1", name="job") as pod:
        assert pod.status == "RUNNING"

    assert ("DELETE", "/pods/pod-1") in client.calls


def test_rental_passes_image_through_to_the_rent():
    """`rental()` takes every `up()` keyword; `image` (lium#161) must reach `_rent`, not the unknown-kwarg guard."""
    client = _Client(ps_sequence=[[_pod("RUNNING")]])

    with client.rental(executor_id="exec-1", name="job", image="repo/img:tag") as pod:
        assert pod.id == "pod-1"

    rent = next(call for call in client.calls if call[0] == "rent")
    assert rent[1]["image"] == "repo/img:tag"


def test_rental_removes_the_pod_when_the_block_raises():
    client = _Client(ps_sequence=[[_pod()]])

    with pytest.raises(RuntimeError, match="job failed"):
        with client.rental(executor_id="exec-1"):
            raise RuntimeError("job failed")

    assert client.calls[-1] == ("DELETE", "/pods/pod-1")


def test_rental_removes_a_pod_that_never_became_ready(monkeypatch):
    client = _Client(ps_sequence=[[_pod("PENDING", None)]])
    clock = iter([0, 0, 1000, 1000, 1000])
    monkeypatch.setattr(time, "time", lambda: next(clock))

    with pytest.raises(LiumError, match="did not become ready"):
        with client.rental(executor_id="exec-1", timeout=5):
            pytest.fail("the block must not run without a ready pod")

    assert ("DELETE", "/pods/pod-1") in client.calls


def test_rental_removes_a_pod_the_server_created_before_the_rent_call_raised():
    """A rent that raised after the server committed it (timed-out second POST, a
    listing that failed) is the case cleanup exists for: the pod that appeared
    during the call is removed, an older same-name pod on the node is not."""
    stale = _pod()
    stale.id, stale.huid = "pod-stale", "old-owl-01"
    fresh = _pod()

    class _RentRaises(_Client):
        def _rent(self, **kwargs):
            self.calls.append(("rent", kwargs))
            raise LiumError("Failed to create pod job")

    client = _RentRaises(ps_sequence=[[stale], [stale, fresh]])

    with pytest.raises(LiumError, match="Failed to create pod"):
        with client.rental(executor_id="exec-1", name="job"):
            pytest.fail("the block must not run when the rent raised")

    assert ("DELETE", "/pods/pod-1") in client.calls
    assert ("DELETE", "/pods/pod-stale") not in client.calls


def test_rental_cleanup_leaves_a_same_name_pod_whose_executor_is_unknown_or_different():
    """Two rentals with the default name can run at once: the one that raised may only
    remove a pod on its own executor. An unknown executor is not a match."""
    unknown = _pod()
    unknown.id, unknown.executor = "pod-unknown", None
    elsewhere = _pod()
    elsewhere.id = "pod-elsewhere"
    elsewhere.executor.id = "exec-2"
    mine = _pod()

    class _RentRaises(_Client):
        def _rent(self, **kwargs):
            raise LiumError("Failed to create pod job")

    client = _RentRaises(ps_sequence=[[], [unknown, elsewhere, mine]])

    with pytest.raises(LiumError):
        with client.rental(executor_id="exec-1", name="job"):
            pass

    deleted = [c[1] for c in client.calls if c[0] == "DELETE"]
    assert deleted == ["/pods/pod-1"]


def test_rental_that_raised_before_any_pod_appeared_removes_nothing():
    class _RentRaises(_Client):
        def _rent(self, **kwargs):
            raise ValueError("bad arguments")

    client = _RentRaises(ps_sequence=[[]])

    with pytest.raises(ValueError):
        with client.rental(executor_id="exec-1", name="job"):
            pass

    assert not [c for c in client.calls if c[0] == "DELETE"]


def test_rental_rejects_an_unknown_argument_before_renting():
    client = _Client()

    with pytest.raises(TypeError, match="wiat"):
        with client.rental(executor_id="exec-1", wiat=True):
            pass

    assert client.calls == []


def test_rental_reports_a_failed_cleanup_without_hiding_the_original_error():
    class _Broken(_Client):
        def _request(self, method, endpoint, **kwargs):
            raise LiumError("Server error: 503")

    client = _Broken(ps_sequence=[[_pod()]])

    with pytest.warns(UserWarning, match="lium rm"):
        with pytest.raises(RuntimeError, match="job failed"):
            with client.rental(executor_id="exec-1"):
                raise RuntimeError("job failed")


# --- exec(timeout=), exec(detach=True) ---------------------------------------------------

class _Channel:
    def __init__(self, ready_after: int, exit_code: int = 0):
        self._polls = 0
        self._ready_after = ready_after
        self.exit_code = exit_code
        self.closed = False

    def exit_status_ready(self):
        self._polls += 1
        return self._polls > self._ready_after

    def recv_exit_status(self):
        return self.exit_code

    def close(self):
        self.closed = True


class _Stream:
    def __init__(self, text: str = "", channel=None):
        self._text = text
        self.channel = channel
        self.written = b""

    def read(self):
        return self._text.encode()

    def write(self, data):
        self.written += data

    def close(self):
        pass


def _ssh_returning(monkeypatch, client, stdout: str, *, exit_code: int = 0, ready_after: int = 0):
    """Replace the SSH session with one that answers every command the same way."""
    channel = _Channel(ready_after, exit_code)
    sent: list[str] = []
    stdin = _Stream()

    class _Ssh:
        def exec_command(self, command, **kwargs):
            sent.append(command)
            return stdin, _Stream(stdout, channel), _Stream("")

    from contextlib import contextmanager

    @contextmanager
    def fake_connection(pod, timeout=30):
        yield _Ssh()

    monkeypatch.setattr(client, "ssh_connection", fake_connection)
    channel.stdin = stdin  # what exec() wrote to the session before closing it
    return sent, channel


def test_exec_with_timeout_returns_when_the_command_finishes(monkeypatch):
    client = _Client()
    _ssh_returning(monkeypatch, client, "done\n", ready_after=2)

    result = client.exec(_pod(), command="echo done", timeout=5)

    assert result == {"stdout": "done\n", "stderr": "", "exit_code": 0, "success": True}


def test_exec_with_timeout_raises_and_closes_the_channel(monkeypatch):
    client = _Client()
    _, channel = _ssh_returning(monkeypatch, client, "", ready_after=10_000)
    clock = iter([0.0, 0.0, 100.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))

    with pytest.raises(TimeoutError, match="sleep 999"):
        client.exec(_pod(), command="sleep 999", timeout=1)

    assert channel.closed


def test_exec_without_timeout_never_polls(monkeypatch):
    client = _Client()
    _, channel = _ssh_returning(monkeypatch, client, "ok\n", ready_after=10_000)

    result = client.exec(_pod(), command="true")

    assert result["success"] and channel._polls == 0


def test_detached_command_line_survives_the_session_and_prints_the_pid():
    line = Lium.build_detached_command("python train.py --lr 1e-4 'a b'", "/workspace/logs/x.log")

    assert "mkdir -p /workspace/logs || exit 1; nohup setsid bash -lc " in line
    assert "'python train.py --lr 1e-4 '\"'\"'a b'\"'\"''" in line
    assert line.endswith("> /workspace/logs/x.log 2>&1 < /dev/null & echo $!")


def test_detached_command_line_checks_for_setsid_and_bash_before_forking():
    """`echo $!` prints a PID as soon as the shell forks, so the check has to come first."""
    line = Lium.build_detached_command("true", "/workspace/logs/x.log")

    assert line.index("command -v") < line.index("mkdir -p") < line.index("nohup")
    assert "setsid" in line[: line.index("mkdir -p")] and "bash" in line[: line.index("mkdir -p")]


def test_exec_detach_returns_pid_and_log_path(monkeypatch):
    client = _Client()
    sent, _ = _ssh_returning(monkeypatch, client, "4242\n")

    result = client.exec(_pod(), command="python train.py", detach=True, log_path="/workspace/logs/t.log")

    assert result == {"pid": 4242, "log_path": "/workspace/logs/t.log", "command": "python train.py"}
    assert "nohup setsid bash -lc 'python train.py'" in sent[0]


def test_exec_detach_picks_a_timestamped_log_by_default(monkeypatch):
    client = _Client()
    _ssh_returning(monkeypatch, client, "7\n")

    result = client.exec(_pod(), command="sleep 1", detach=True)

    assert result["log_path"].startswith("/workspace/logs/exec-")
    assert re.fullmatch(r"/workspace/logs/exec-\d{8}T\d{6}Z-[0-9a-f]{6}\.log", result["log_path"])


def test_detach_tokens_from_the_same_second_do_not_collide():
    """Two jobs started in the same second must not share (and truncate) one log file: the token
    is the UTC stamp of the given instant plus a random tail. A token that were the stamp alone
    would make every set below collapse to one path."""
    from lium.sdk import detach

    same_second = 1_800_000_000.0
    paths = {detach.default_detach_log_path(detach.detach_token(now=same_second)) for _ in range(16)}

    assert len(paths) == 16
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(same_second))
    assert all(re.fullmatch(rf"/workspace/logs/exec-{stamp}-[0-9a-f]{{6}}\.log", p) for p in paths)


def test_exec_detach_sends_env_over_stdin_and_applies_it_inside_the_login_shell(monkeypatch):
    """The value never sits in the pod's argv (DAH-2984 applies to detach too): the
    exports travel over stdin as one variable, and the detached `bash -lc` applies
    them after its profile, so the given value wins over a profile assignment."""
    client = _Client()
    sent, channel = _ssh_returning(monkeypatch, client, "7\n")

    client.exec(_pod(), command="run", env={"HF_HOME": "/workspace/hf"}, detach=True)

    assert sent[0].startswith('eval "$(cat)" && ')
    assert "/workspace/hf" not in sent[0]
    assert "nohup setsid bash -lc 'eval \"$LIUM_JOB_ENV\" || exit 1; unset LIUM_JOB_ENV; run'" in sent[0]
    assert channel.stdin.written == b"export LIUM_JOB_ENV='export HF_HOME=/workspace/hf'"


def test_exec_detach_fails_loudly_when_no_pid_came_back(monkeypatch):
    client = _Client()
    _ssh_returning(monkeypatch, client, "", exit_code=1)

    with pytest.raises(LiumError, match="detached command"):
        client.exec(_pod(), command="run", detach=True)


def test_log_path_without_detach_is_rejected():
    with pytest.raises(ValueError, match="detach"):
        _Client().exec(_pod(), command="x", log_path="/tmp/x.log")


# --- gpu_stats ------------------------------------------------------------------------------

NVIDIA_SMI = (
    "0, NVIDIA H100 80GB HBM3, 97, 65432, 81559, 61, 512.30\n"
    "1, NVIDIA H100 80GB HBM3, 0, 1, 81559, 30, [N/A]\n"
)


def test_parse_gpu_stats_reads_every_field_and_tolerates_missing_sensors():
    stats = Lium.parse_gpu_stats(NVIDIA_SMI)

    assert [s.index for s in stats] == [0, 1]
    assert stats[0].name == "NVIDIA H100 80GB HBM3"
    assert stats[0].utilization_pct == 97
    assert stats[0].memory_used_mib == 65432 and stats[0].memory_total_mib == 81559
    assert stats[0].memory_pct == 80.2
    assert stats[0].temperature_c == 61 and stats[0].power_draw_w == 512.3
    assert stats[1].power_draw_w is None


def test_parse_gpu_stats_skips_lines_that_are_not_readings():
    assert Lium.parse_gpu_stats("nvidia-smi: command not found\n") == []


def test_gpu_stats_runs_the_query_and_parses_it(monkeypatch):
    client = _Client()
    sent, _ = _ssh_returning(monkeypatch, client, NVIDIA_SMI)

    stats = client.gpu_stats(_pod())

    assert sent == [Lium.GPU_QUERY_COMMAND]
    assert "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw" in sent[0]
    assert len(stats) == 2 and isinstance(stats[0], GpuStats)


def test_gpu_stats_reports_a_failed_nvidia_smi(monkeypatch):
    client = _Client()
    _ssh_returning(monkeypatch, client, "", exit_code=127)

    with pytest.raises(LiumError, match="nvidia-smi failed"):
        client.gpu_stats(_pod())


# --- to_dict, schema ---------------------------------------------------------------------------

def test_to_dict_is_json_serialisable_and_carries_derived_fields():
    """A caller `json.dumps` what the SDK returns: the nested executor dataclass must be converted
    (a raw dataclass is not serialisable) and the properties a user reads on the object — the
    pod's host/username/port, the executor's GPU model — must be in the dict too, equal to what
    the object reports, so dropping `_derived()` from `to_dict()` or the nested conversion fails this."""
    pod = _pod()

    data = pod.to_dict()

    assert json.loads(json.dumps(data)) == data
    assert data["executor"]["gpu_model"] == pod.executor.gpu_model
    assert (data["host"], data["username"], data["ssh_port"]) == (pod.host, pod.username, pod.ssh_port)
    assert data["executor"]["huid"] == pod.executor.huid


def test_to_dict_exists_on_every_serialisable_model():
    """Every model the `_Serializable` mixin covers has `to_dict()`; `RentResult` and the workspace
    models are plain dataclasses and must not be advertised as serialisable."""
    from lium.sdk.models import BackupConfig, BackupLog, RentResult, RestoreLog, SSHKey, VolumeInfo, WorkspaceInfo

    for model in (PodInfo, ExecutorInfo, Template, GpuStats, BackupConfig, BackupLog, RestoreLog, SSHKey, VolumeInfo):
        assert callable(getattr(model, "to_dict", None)), model.__name__
    for model in (RentResult, WorkspaceInfo):
        assert not hasattr(model, "to_dict"), model.__name__
