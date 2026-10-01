"""`lium exec` keeps one OpenSSH connection per pod across runs, and skips the pod list when it is up."""
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.commands import exec as exec_module
from lium.cli.commands.exec import exec_command, pods_with_live_masters
from lium.cli.utils import POD_CACHE_TTL_SECONDS, remember_pods, remembered_pods
from lium.sdk import Config, Lium, LiumError, LiumHostKeyError, PodInfo, ssh_mux


def _pod(pod_id="pod-1", name="warm-pod", huid="eager-wolf-aa", ssh_cmd="ssh root@203.0.113.10 -p 20299"):
    return PodInfo(
        id=pod_id, name=name, huid=huid, status="RUNNING", ssh_cmd=ssh_cmd, ports={}, created_at="",
        updated_at="", executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


@pytest.fixture
def home(monkeypatch):
    short = Path(tempfile.mkdtemp(prefix="lm", dir="/tmp"))   # pytest's tmp_path is too long for a unix socket
    monkeypatch.setenv("HOME", str(short))
    monkeypatch.setenv(ssh_mux.PERSIST_ENV, "")
    monkeypatch.delenv("LIUM_SSH_INSECURE", raising=False)
    monkeypatch.setattr(ssh_mux.shutil, "which", lambda name: "/usr/bin/ssh")
    yield short
    shutil.rmtree(short, ignore_errors=True)


def _lium(tmp_path):
    return Lium(Config(api_key="test-key", ssh_key_path=tmp_path / "id_ed25519"))


class _Ssh:
    """Stands in for the ssh binary: records argv and stdin, answers -O check from ``live``."""

    def __init__(self, live=True, exit_code=0, stderr=b"", start_stderr=b"", start_code=0):
        self.calls = []
        self.live, self.exit_code, self.stderr = live, exit_code, stderr
        self.start_stderr, self.start_code = start_stderr, start_code

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        path = Path(next(a for a in argv if a.startswith("ControlPath=")).split("=", 1)[1])
        if "check" in argv:
            return SimpleNamespace(returncode=0 if self.live else 255)
        if "exit" in argv:
            self.live = False
            return SimpleNamespace(returncode=0)
        if "ControlMaster=yes" in argv:
            kwargs["stderr"].write(self.start_stderr)
            if self.start_code == 0:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.live = True
            return SimpleNamespace(returncode=self.start_code)
        return SimpleNamespace(returncode=self.exit_code, stdout=b"out\n", stderr=self.stderr)

    def commands(self):
        return [argv for argv, _ in self.calls if "ControlMaster=no" in argv]


def test_the_control_socket_is_per_pod_and_ssh_address(home):
    first = ssh_mux.control_path(_pod())
    assert first.parent == home / ".lium" / "ssh-mux"
    assert first != ssh_mux.control_path(_pod(ssh_cmd="ssh root@203.0.113.10 -p 20300"))
    assert first != ssh_mux.control_path(_pod(pod_id="pod-2"))


def test_a_home_too_long_for_a_unix_socket_puts_the_socket_under_the_temp_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / ("x" * 80)))
    assert ssh_mux.socket_dir().parent == Path(ssh_mux.tempfile.gettempdir())


@pytest.mark.parametrize("value, seconds", [("", 600), ("30", 30), ("0", 0), ("soon", 600)])
def test_persist_seconds_reads_lium_ssh_persist(monkeypatch, value, seconds):
    monkeypatch.setenv(ssh_mux.PERSIST_ENV, value)
    assert ssh_mux.persist_seconds() == seconds


def test_persist_zero_or_no_ssh_binary_turns_the_master_off(home, monkeypatch):
    assert ssh_mux.available()
    monkeypatch.setenv(ssh_mux.PERSIST_ENV, "0")
    assert not ssh_mux.available()
    monkeypatch.setenv(ssh_mux.PERSIST_ENV, "")
    monkeypatch.setattr(ssh_mux.shutil, "which", lambda name: None)
    assert not ssh_mux.available()


def test_the_first_command_starts_a_background_master_with_the_pods_pinned_key(home, monkeypatch):
    ssh = _Ssh(live=False)
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    pod = _pod()

    result = ssh_mux.exec_over_master(_lium(home), pod, command="nvidia-smi")

    start = next(argv for argv, _ in ssh.calls if "ControlMaster=yes" in argv)
    assert {"-N", "-f", "ControlPersist=600", "BatchMode=yes", "IdentitiesOnly=yes"} <= set(start)
    assert "StrictHostKeyChecking=accept-new" in start
    assert f'UserKnownHostsFile="{home / ".lium" / "known_hosts" / "pod-1"}"' in start
    assert start[-2:] == ["--", "root@203.0.113.10"] and start[start.index("-p") + 1] == "20299"
    assert result == {"stdout": "out\n", "stderr": "", "exit_code": 0, "success": True}
    assert oct((home / ".lium" / "ssh-mux").stat().st_mode & 0o777) == "0o700"


def test_a_command_goes_over_the_live_master_and_env_values_travel_on_stdin(home, monkeypatch):
    ssh = _Ssh(live=True)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    ssh_mux.control_path(_pod()).touch()
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)

    ssh_mux.exec_over_master(_lium(home), _pod(), command="-v; python app.py", env={"TOKEN": "s3cret"})

    assert not any("ControlMaster=yes" in argv for argv, _ in ssh.calls)
    [argv] = ssh.commands()
    assert argv[-3:] == ["--", "root@203.0.113.10", 'eval "$(cat)" && -v; python app.py']
    assert "s3cret" not in " ".join(argv)
    stdin = next(kwargs["input"] for a, kwargs in ssh.calls if a is argv)
    assert b"s3cret" in stdin


def test_a_bad_env_name_is_refused_before_ssh_runs(home, monkeypatch):
    ssh = _Ssh()
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    with pytest.raises(ValueError):
        ssh_mux.exec_over_master(_lium(home), _pod(), command="true", env={"1BAD": "x"})
    assert ssh.calls == []


def test_exit_255_with_the_master_gone_is_a_lost_connection(home, monkeypatch):
    ssh = _Ssh(live=True, exit_code=255, stderr=b"Connection to 203.0.113.10 closed by remote host.\n")
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    ssh_mux.control_path(_pod()).touch()
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    original = ssh.__call__

    def dies_during_the_command(argv, **kwargs):
        done = original(argv, **kwargs)
        if "ControlMaster=no" in argv:
            ssh.live = False
        return done

    monkeypatch.setattr(ssh_mux.subprocess, "run", dies_during_the_command)
    with pytest.raises(LiumError, match="SSH connection to pod warm-pod was lost: Connection to"):
        ssh_mux.exec_over_master(_lium(home), _pod(), command="train.py")


def test_exit_255_from_the_command_itself_is_its_exit_code(home, monkeypatch):
    ssh = _Ssh(live=True, exit_code=255)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    ssh_mux.control_path(_pod()).touch()
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)

    assert ssh_mux.exec_over_master(_lium(home), _pod(), command="exit 255")["exit_code"] == 255


def test_a_changed_host_key_is_a_host_key_error(home, monkeypatch):
    ssh = _Ssh(live=False, start_code=255,
               start_stderr=b"@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@\nHost key verification failed.\n")
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    with pytest.raises(LiumHostKeyError, match="known_hosts"):
        ssh_mux.exec_over_master(_lium(home), _pod(), command="true")
    assert ssh.commands() == []


def test_an_unreachable_pod_names_sshs_reason(home, monkeypatch):
    ssh = _Ssh(live=False, start_code=255, start_stderr=b"ssh: connect to host 203.0.113.10 port 20299: Connection refused\n")
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    with pytest.raises(LiumError, match="SSH to pod warm-pod .* Connection refused"):
        ssh_mux.exec_over_master(_lium(home), _pod(), command="true")


def test_exec_all_over_masters_reports_one_unreachable_pod_as_one_failed_entry(home, monkeypatch):
    good, bad = _pod(), _pod(pod_id="pod-2", name="gone", ssh_cmd="ssh root@203.0.113.99 -p 22")

    def one(lium, pod, *, command, env=None):
        if pod is bad:
            raise LiumError("SSH to pod gone failed")
        return {"stdout": "", "stderr": "", "exit_code": 0, "success": True}

    monkeypatch.setattr(ssh_mux, "exec_over_master", one)
    results = ssh_mux.exec_all_over_masters(_lium(home), [good, bad], command="true")
    assert results == [
        {"stdout": "", "stderr": "", "exit_code": 0, "success": True, "pod": "pod-1"},
        {"pod": "pod-2", "error": "SSH to pod gone failed", "success": False},
    ]


def test_a_socket_in_a_directory_open_to_others_is_never_used(home, monkeypatch):
    ssh = _Ssh(live=True)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    path = ssh_mux.control_path(_pod())
    path.touch()
    assert ssh_mux.socket_is_ours(path)
    os.chmod(path.parent, 0o755)
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)

    assert not ssh_mux.socket_is_ours(path)
    assert not ssh_mux.has_live_master(_lium(home), _pod())
    ssh_mux.stop(_lium(home), _pod())
    assert ssh.calls == []                        # nothing was sent to that socket


def test_starting_a_master_replaces_a_socket_left_at_its_path(home, monkeypatch):
    ssh = _Ssh(live=False)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    path = ssh_mux.control_path(_pod())
    path.write_text("planted")
    os.chmod(path.parent, 0o777)
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)

    ssh_mux.exec_over_master(_lium(home), _pod(), command="true")

    assert path.read_text() == ""                 # the fake master made a new one
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert [argv for argv, _ in ssh.calls if "check" in argv] == []   # the open directory was never trusted


@pytest.mark.parametrize("operation", ["down", "reboot"])
def test_removing_or_rebooting_a_pod_closes_its_master(home, monkeypatch, operation):
    ssh = _Ssh(live=True)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    ssh_mux.control_path(_pod()).touch()
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    lium = _lium(home)
    monkeypatch.setattr(lium, "_request", lambda *a, **k: SimpleNamespace(json=lambda: {}))

    getattr(lium, operation)(_pod())

    assert any("exit" in argv for argv, _ in ssh.calls) and not ssh.live


# -- lium exec ------------------------------------------------------------------------------------


def test_the_pod_cache_is_per_account_private_and_expires(home):
    lium = _lium(home)
    now = datetime.now(timezone.utc)
    remember_pods(lium, [_pod()], now=now)

    [pod] = remembered_pods(lium, now=now)
    assert (pod.id, pod.name, pod.huid, pod.ssh_cmd) == ("pod-1", "warm-pod", "eager-wolf-aa", _pod().ssh_cmd)
    other_account = Lium(Config(api_key="other-key", ssh_key_path=home / "id_ed25519"))
    assert remembered_pods(other_account, now=now) is None
    assert remembered_pods(lium, now=now + timedelta(seconds=POD_CACHE_TTL_SECONDS + 1)) is None
    [path] = (home / ".lium" / "pod_cache").iterdir()
    assert oct(path.stat().st_mode & 0o777) == "0o600" and "test-key" not in path.read_text()


@pytest.mark.parametrize("targets", ["all", "1", "warm-pod,2", "someone-else"])
def test_targets_the_cache_cannot_answer_go_to_the_pod_list(home, monkeypatch, targets):
    lium = _lium(home)
    remember_pods(lium, [_pod()])
    monkeypatch.setattr(ssh_mux, "has_live_master", lambda lium, pod: True)
    assert pods_with_live_masters(lium, targets) is None


def test_a_cached_pod_without_a_live_master_goes_to_the_pod_list(home, monkeypatch):
    lium = _lium(home)
    remember_pods(lium, [_pod()])
    monkeypatch.setattr(ssh_mux, "has_live_master", lambda lium, pod: False)
    assert pods_with_live_masters(lium, "warm-pod") is None


def test_cached_pods_with_live_masters_are_used_by_name_huid_or_id(home, monkeypatch):
    lium = _lium(home)
    remember_pods(lium, [_pod(), _pod(pod_id="pod-2", name="other", huid="calm-owl-bb")])
    monkeypatch.setattr(ssh_mux, "has_live_master", lambda lium, pod: True)
    assert [p.id for p in pods_with_live_masters(lium, "warm-pod, calm-owl-bb,pod-1")] == ["pod-1", "pod-2", "pod-1"]


class _CliLium:
    pods = []
    ps_calls = 0

    def __init__(self, *args, **kwargs):
        self.config = Config(api_key="test-key", ssh_key_path=Path("/keys/id_ed25519"))

    def ps(self):
        type(self).ps_calls += 1
        return list(self.pods)

    def exec(self, pod, **kwargs):
        raise AssertionError("with a control master available, lium exec runs over it")


def _run_cli(monkeypatch, *args):
    sent = []

    def over_master(lium, pod, *, command, env=None):
        sent.append((pod.id, command, env))
        return {"stdout": "hi\n", "stderr": "", "exit_code": 0, "success": True}

    _CliLium.ps_calls = 0
    monkeypatch.setattr(exec_module, "Lium", _CliLium)
    monkeypatch.setattr(ssh_mux, "exec_over_master", over_master)
    result = CliRunner().invoke(exec_command, list(args))
    return result, sent


def test_lium_exec_on_a_pod_with_a_live_master_lists_no_pods(home, monkeypatch):
    _CliLium.pods = [_pod()]
    live = set()
    monkeypatch.setattr(ssh_mux, "has_live_master", lambda lium, pod: pod.id in live)

    first, sent = _run_cli(monkeypatch, "warm-pod", "echo hi", "-e", "K=v")
    assert first.exit_code == 0 and _CliLium.ps_calls == 1 and sent == [("pod-1", "echo hi", {"K": "v"})]

    live.add("pod-1")
    second, sent = _run_cli(monkeypatch, "warm-pod", "echo hi")
    assert second.exit_code == 0 and _CliLium.ps_calls == 0 and sent == [("pod-1", "echo hi", {})]
    assert "hi" in second.output


def test_lium_exec_with_persist_off_runs_over_the_sdk(home, monkeypatch):
    monkeypatch.setenv(ssh_mux.PERSIST_ENV, "0")
    _CliLium.pods = [_pod()]
    calls = []
    monkeypatch.setattr(_CliLium, "exec", lambda self, pod, **kw: calls.append(kw) or
                        {"stdout": "", "stderr": "", "exit_code": 0, "success": True})

    result, sent = _run_cli(monkeypatch, "warm-pod", "true")

    assert result.exit_code == 0 and calls and sent == []
    assert not (home / ".lium" / "pod_cache").exists()


# -- against a real sshd --------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_warm_exec  # noqa: E402

_no_sshd = bench_warm_exec.find_sshd() is None or shutil.which("ssh") is None or shutil.which("ssh-keygen") is None


@pytest.fixture
def bench():
    try:
        b = bench_warm_exec.Bench(rtt_ms=0, api_ms=0)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"no local sshd: {e}")
    try:
        yield b
    finally:
        b.close()


@pytest.mark.skipif(_no_sshd, reason="needs sshd, ssh and ssh-keygen")
def test_repeated_lium_exec_opens_one_ssh_connection_and_lists_pods_once(bench):
    result = bench_warm_exec.bench_cli(bench, sys.executable, 3, {})
    assert result["ssh_connections"] == 1
    assert result["api_by_route"] == {"GET /pods": 1}


@pytest.mark.skipif(_no_sshd, reason="needs sshd, ssh and ssh-keygen")
def test_lium_exec_with_persist_off_connects_and_lists_pods_per_run(bench):
    result = bench_warm_exec.bench_cli(bench, sys.executable, 2, {ssh_mux.PERSIST_ENV: "0"})
    assert result["ssh_connections"] == 2
    assert result["api_by_route"] == {"GET /pods": 2}


@pytest.mark.skipif(_no_sshd, reason="needs sshd, ssh and ssh-keygen")
def test_repeated_sdk_exec_opens_one_ssh_connection(bench):
    result = bench_warm_exec.bench_sdk(bench, sys.executable, 3, {})
    assert result["ssh_connections"] == 1
