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
from lium.cli.commands.exec import exec_command
from lium.cli.utils import POD_CACHE_TTL_SECONDS, remember_pods, remembered_pods
from lium.sdk import Config, Lium, PodInfo, ssh_mux


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
    # commands go through the (faked) subprocess.run, so the fakes below see argv and input; _capture has its own tests
    monkeypatch.setattr(ssh_mux, "_capture", _capture_via_run)
    yield short
    shutil.rmtree(short, ignore_errors=True)


def _capture_via_run(argv, stdin, max_output_bytes):
    done = ssh_mux.subprocess.run(argv, input=stdin, capture_output=True)
    return done.returncode, done.stdout, done.stderr, len(done.stdout) + len(done.stderr) > max_output_bytes


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
        path = Path(next(a for a in argv if a.startswith("ControlPath=")).split("=", 1)[1].strip('"'))
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




def test_the_control_socket_changes_with_the_host_key_policy_and_the_key(home, monkeypatch):
    secure = ssh_mux.control_path(_lium(home), _pod())
    assert secure != ssh_mux.control_path(Lium(Config(api_key="test-key", ssh_key_path=home / "other")), _pod())
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    insecure = ssh_mux.control_path(_lium(home), _pod())
    assert insecure != secure and len(insecure.name) == len(secure.name) == 24


def test_a_master_opened_without_host_key_checks_is_not_reused_once_they_are_back_on(home, monkeypatch):
    ssh = _Ssh(live=False)
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    with pytest.warns(UserWarning):
        ssh_mux.exec_over_master(_lium(home), _pod(), command="true")
    monkeypatch.delenv("LIUM_SSH_INSECURE")
    ssh_mux.exec_over_master(_lium(home), _pod(), command="true")

    starts = [argv for argv, _ in ssh.calls if "ControlMaster=yes" in argv]
    paths = {next(a for a in argv if a.startswith("ControlPath=")) for argv in starts}
    assert len(starts) == 2 and len(paths) == 2
    assert "StrictHostKeyChecking=accept-new" in starts[1]










@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client")
@pytest.mark.parametrize("option, expected", [
    ("forwardagent", "no"),
    ("forwardx11", "no"),
    ("clearallforwardings", "yes"),
])
def test_the_renters_ssh_config_cannot_forward_anything_to_the_pod(home, option, expected):
    config = home / "ssh_config"
    config.write_text("Host *\n  ForwardAgent yes\n  ForwardX11 yes\n  ForwardX11Trusted yes\n  LocalForward 8888 localhost:8888\n")
    master = ssh_mux.ControlMaster(_lium(home), _pod())

    evaluated = subprocess.run(["ssh", "-F", str(config), "-G", *master.base[1:], "--", master.destination],
                               capture_output=True, text=True, check=True).stdout

    assert f"{option} {expected}" in evaluated.splitlines()


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the OpenSSH client")
def test_the_renters_ssh_config_cannot_send_environment_values_to_the_pod(home):
    config = home / "ssh_config"
    config.write_text("Host *\n  SendEnv AWS_* SECRET\n  SetEnv TOKEN=local-secret\n")
    master = ssh_mux.ControlMaster(_lium(home), _pod())

    # the renter's config comes first, as ssh's own default would; the master's argv must override it
    evaluated = subprocess.run(["ssh", "-F", str(config), "-G", *master.base[1:], "--", master.destination],
                               capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL).stdout.lower()

    assert "sendenv" not in evaluated and "setenv" not in evaluated




















def test_a_socket_in_a_directory_open_to_others_is_never_used(home, monkeypatch):
    ssh = _Ssh(live=True)
    ssh_mux.ensure_socket_dir(ssh_mux.socket_dir())
    path = ssh_mux.control_path(_lium(home), _pod())
    path.touch()
    assert ssh_mux.socket_is_ours(path)
    os.chmod(path.parent, 0o755)
    monkeypatch.setattr(ssh_mux.subprocess, "run", ssh)

    assert not ssh_mux.socket_is_ours(path)
    assert not ssh_mux.has_live_master(_lium(home), _pod())
    ssh_mux.stop(_lium(home), _pod())
    assert ssh.calls == []                        # nothing was sent to that socket










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

    def over_master(lium, pod, *, command, env=None, max_output_bytes=None):
        sent.append((pod.id, command, env))
        return {"stdout": "hi\n", "stderr": "", "exit_code": 0, "success": True}

    _CliLium.ps_calls = 0
    monkeypatch.setattr(exec_module, "Lium", _CliLium)
    monkeypatch.setattr(ssh_mux, "exec_over_master", over_master)
    result = CliRunner().invoke(exec_command, list(args))
    return result, sent




def test_lium_exec_script_never_puts_its_text_in_ssh_argv(home, monkeypatch, tmp_path):
    _CliLium.pods = [_pod()]
    monkeypatch.setattr(ssh_mux, "has_live_master", lambda lium, pod: True)
    calls = []
    monkeypatch.setattr(_CliLium, "exec", lambda self, pod, **kw: calls.append(kw["command"]) or
                        {"stdout": "", "stderr": "", "exit_code": 0, "success": True})
    script = tmp_path / "s.sh"
    script.write_text("echo SECRET-IN-SCRIPT\n")

    result, sent = _run_cli(monkeypatch, "eager-wolf-aa", "--script", str(script))
    assert result.exit_code == 0, result.output
    assert sent == [] and len(calls) == 1 and "SECRET-IN-SCRIPT" in calls[0]




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










def test_a_symlinked_socket_dir_is_refused_and_its_target_left_alone(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    victim.chmod(0o755)
    link = tmp_path / "lium-ssh-mux"
    link.symlink_to(victim)

    with pytest.raises(ssh_mux.LiumError):
        ssh_mux.ensure_socket_dir(link)

    assert (victim.stat().st_mode & 0o777) == 0o755


_OUTPUT_600_OUT_401_ERR = [sys.executable, "-c", "import os; os.write(1, b'o' * 600); os.write(2, b'e' * 401)"]








