"""`lium up` and `lium ssh` wait for the pod's SSH banner before opening the session.

A pod reports RUNNING before an sshd the image starts itself is listening; the node's port
forward accepts the TCP connection meanwhile and closes it. Both commands made one ssh attempt,
so that start-up race failed. They now poll the SSH port for up to 60 s for the server's `SSH-`
line, then open the session (still tried once when the wait runs out). The pod is never removed.
"""

import json
import socket
import threading
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.ssh import actions as ssh_actions
from lium.cli.ssh import command as ssh_module
from lium.cli.up import command as up_module
from lium.cli.utils import EXIT_SSH_ERROR
from lium.sdk import PodInfo


def _pod(ssh_cmd="ssh root@203.0.113.10 -p 20022") -> PodInfo:
    return PodInfo(
        id="pod-1", name="train", status="RUNNING", huid="eager-wolf-aa",
        ssh_cmd=ssh_cmd, ports={"22": 20022}, created_at="2026-09-05T10:00:00Z",
        updated_at="2026-09-05T10:00:00Z", executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None, gpu_count=1,
    )


def _flat(text: str) -> str:
    """Output with Rich's line wrapping undone."""
    return " ".join(text.split())


# --- ssh_banner_problem against a real socket --------------------------------------------------

@pytest.fixture
def server():
    """A one-connection TCP server on localhost; ``start(handler)`` returns its port."""
    listeners = []

    def start(handler):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        listeners.append(sock)

        def serve():
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            with conn:
                handler(conn)

        threading.Thread(target=serve, daemon=True).start()
        return sock.getsockname()[1]

    yield start
    for sock in listeners:
        sock.close()


def test_an_ssh_server_is_ready(server):
    port = server(lambda conn: conn.sendall(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13\r\n"))

    assert ssh_actions.ssh_banner_problem("127.0.0.1", port) is None


def test_lines_before_the_ssh_line_are_allowed(server):
    port = server(lambda conn: conn.sendall(b"Welcome to the node\r\nSSH-2.0-dropbear\r\n"))

    assert ssh_actions.ssh_banner_problem("127.0.0.1", port) is None


def test_a_forward_that_accepts_and_closes_is_not_ready(server):
    """What a docker port forward does while nothing listens inside the container."""
    port = server(lambda conn: None)

    assert ssh_actions.ssh_banner_problem("127.0.0.1", port) == "connection closed before an SSH banner"


def test_a_silent_port_is_not_ready(server):
    release = threading.Event()
    port = server(lambda conn: release.wait(5))

    try:
        assert ssh_actions.ssh_banner_problem("127.0.0.1", port, timeout=0.2) == "no answer"
    finally:
        release.set()


def test_another_protocol_is_not_ready(server):
    port = server(lambda conn: conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n"))

    assert ssh_actions.ssh_banner_problem("127.0.0.1", port) == "not an SSH server"


def test_a_refused_port_is_not_ready():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    assert ssh_actions.ssh_banner_problem("127.0.0.1", port) == "connection refused"


# --- WaitForSSHAction ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _wait(probe, clock, **ctx):
    return ssh_actions.WaitForSSHAction().execute(
        {"pod": _pod(), "probe": probe, "sleep": clock.sleep, "clock": clock, **ctx}
    )


def test_a_ready_port_is_not_waited_on():
    clock = _Clock()

    result = _wait(lambda h, p: None, clock)

    assert result.ok
    assert clock.sleeps == []


def test_the_wait_retries_until_the_banner_comes():
    clock = _Clock()
    answers = iter(["connection refused", "connection closed before an SSH banner", None])
    probed = []

    def probe(host, port):
        probed.append((host, port))
        return next(answers)

    result = _wait(probe, clock)

    assert result.ok
    assert result.data == {"host": "203.0.113.10", "port": 20022, "attempts": 3, "wait_seconds": 60}
    assert probed == [("203.0.113.10", 20022)] * 3
    assert clock.sleeps == [ssh_actions.SSH_READY_INTERVAL] * 2


def test_the_wait_gives_up_after_sixty_seconds_and_says_what_the_port_did():
    clock = _Clock()

    result = _wait(lambda h, p: "connection refused", clock)

    assert not result.ok
    assert ssh_actions.SSH_READY_SECONDS == 60
    assert 60 - ssh_actions.SSH_READY_INTERVAL < sum(clock.sleeps) <= 60
    assert result.data["last_problem"] == "connection refused"
    assert result.data["attempts"] == len(clock.sleeps) + 1
    assert result.error == "203.0.113.10:20022 gave no SSH banner within 60s (connection refused)"


def test_the_wait_counts_slow_probes_against_the_deadline():
    """A probe that hangs for its whole timeout still ends the wait at about 60 s, not 60 s of sleeps."""
    clock = _Clock()

    def slow_probe(host, port):
        clock.now += ssh_actions.SSH_PROBE_TIMEOUT
        return "no answer"

    result = _wait(slow_probe, clock)

    assert not result.ok
    assert clock.now - 1000.0 <= 60 + ssh_actions.SSH_PROBE_TIMEOUT


def test_the_wait_respects_a_shorter_timeout():
    clock = _Clock()

    result = _wait(lambda h, p: "connection refused", clock, wait_seconds=10)

    assert not result.ok
    assert sum(clock.sleeps) <= 10
    assert "within 10s" in result.error


# --- shared harness -----------------------------------------------------------------------------

def _patch_probe(monkeypatch, calls, probe):
    """Route the real wait through ``probe`` on a fake clock; return the clock."""
    clock = _Clock()

    def _probe(host, port):
        calls.append(("probe", host, port))
        return probe(host, port)

    monkeypatch.setattr(ssh_actions, "ssh_banner_problem", _probe)
    monkeypatch.setattr(ssh_actions.time, "monotonic", clock)
    monkeypatch.setattr(ssh_actions.time, "sleep", clock.sleep)
    return clock


def _envelope(result) -> dict:
    return next(json.loads(line) for line in reversed(result.stderr.strip().splitlines()) if line.startswith("{"))


# --- lium up ------------------------------------------------------------------------------------

def _run_up(monkeypatch, *, probe, ssh_connects, json_output=False):
    """`lium up brave-fox-3a --yes` against fakes, through to the SSH session; returns (result, calls)."""
    calls: list = []
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000",
        price_per_hour=0.24, available_port_count=10, download_speed=1000,
    )

    class _Lium:
        workspaces = SimpleNamespace(current=lambda: None)

        def get_deployment_estimate(self, *a, **k):
            return {}

        def rm(self, pod):
            calls.append(("rm", pod.id))

        def down(self, pod):
            calls.append(("rm", pod.id))

    def _action(fn):
        return lambda: SimpleNamespace(execute=fn)

    def _ssh(argv):
        calls.append(("ssh", tuple(argv)))
        return ssh_connects

    monkeypatch.setattr(up_module, "ensure_config", lambda: None)
    monkeypatch.setattr(up_module, "Lium", lambda **kwargs: _Lium())
    monkeypatch.setattr(up_module, "ResolveExecutorAction",
                        _action(lambda ctx: ActionResult(ok=True, data={"executor": executor})))
    monkeypatch.setattr(up_module, "ResolveTemplateAction",
                        _action(lambda ctx: ActionResult(ok=True, data={"template": SimpleNamespace(id="tmpl-1")})))
    monkeypatch.setattr(up_module, "RentPodAction", _action(lambda ctx: ActionResult(
        ok=True, data={"pod_info": {"id": "pod-1"}, "pod_id": "pod-1", "pod_name": "train"})))
    monkeypatch.setattr(up_module, "WaitReadyAction", _action(lambda ctx: ActionResult(ok=True, data={"pod": _pod()})))
    monkeypatch.setattr(up_module, "VerifyGpuCountAction", _action(lambda ctx: ActionResult(ok=True, data={})))
    monkeypatch.setattr(up_module, "PrepareSSHAction", _action(lambda ctx: ActionResult(
        ok=True, data={"ssh_argv": ["ssh", "-p", "20022", "root@203.0.113.10"], "pod": _pod()})))
    monkeypatch.setattr(ssh_module, "ssh_session_connected", _ssh)
    _patch_probe(monkeypatch, calls, probe)

    args = ["up", "brave-fox-3a", "--yes"] + (["--json"] if json_output else [])
    return CliRunner().invoke(cli, args), calls


def test_up_opens_the_session_at_once_when_sshd_answers(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=True)

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["probe", "ssh"]


def test_up_opens_the_session_once_sshd_answers(monkeypatch):
    answers = iter(["connection refused", "connection refused", None])

    result, calls = _run_up(monkeypatch, probe=lambda h, p: next(answers), ssh_connects=True)

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["probe", "probe", "probe", "ssh"]
    assert calls[0] == ("probe", "203.0.113.10", 20022)


def test_up_still_tries_ssh_when_the_port_never_answers_the_probe(monkeypatch):
    """The probe connects directly; a route in the user's ssh config may still work."""
    result, calls = _run_up(monkeypatch, probe=lambda h, p: "connection refused", ssh_connects=True)

    assert result.exit_code == 0, result.output
    assert calls[-1][0] == "ssh"
    assert "gave no SSH banner within 60s" in _flat(result.output)
    assert ("rm", "pod-1") not in calls


def test_up_fails_without_removing_the_pod_when_ssh_never_comes_up(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=lambda h, p: "connection refused", ssh_connects=False)

    output = _flat(result.output)
    assert result.exit_code == EXIT_SSH_ERROR
    assert ("rm", "pod-1") not in calls
    assert ("Pod eager-wolf-aa is RUNNING and billing, but SSH did not answer within 60s "
            "(203.0.113.10:20022: connection refused)") in output
    assert "'lium ssh eager-wolf-aa'" in output
    assert "'lium rm eager-wolf-aa'" in output
    assert "The pod was not removed" in output


def test_up_failure_envelope_names_the_pod_and_the_wait(monkeypatch):
    """The agent-facing envelope: the pod that bills and why the port was judged down."""
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_up(monkeypatch, probe=lambda h, p: "connection refused", ssh_connects=False)

    envelope = _envelope(result)
    assert result.exit_code == EXIT_SSH_ERROR
    assert ("rm", "pod-1") not in calls
    assert envelope["error"]["code"] == "ssh_connection_failed"
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["ssh_port_answered"] is False
    assert envelope["data"]["ssh_wait"]["last_problem"] == "connection refused"


def test_up_keeps_the_old_failure_when_the_port_answered(monkeypatch):
    """Banner seen but ssh still refused: a key problem, so the key hint stays."""
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=False)

    assert result.exit_code == EXIT_SSH_ERROR
    assert "is running but the SSH connection failed" in _flat(result.output)
    assert "did not answer within" not in _flat(result.output)


def test_up_json_does_not_probe_ssh(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=True, json_output=True)

    assert result.exit_code == 0, result.output
    assert not [c for c in calls if c[0] in ("probe", "ssh")]


# --- lium ssh -----------------------------------------------------------------------------------

def _run_ssh(monkeypatch, *, probe, returncode, pod=None):
    """`lium ssh eager-wolf-aa` against a fake pod list; returns (result, calls)."""
    calls: list = []
    target = pod or _pod()

    class _Lium:
        def __init__(self, *a, **kw):
            pass

        def ps(self):
            return [target]

        def ssh_argv(self, pod):
            from lium.sdk.client import ssh_target
            user, host, port = ssh_target(pod.ssh_cmd)
            return ["ssh", "-p", str(port), f"{user}@{host}"]

        def rm(self, pod):
            calls.append(("rm", pod.id))

    def _run(argv, **kwargs):
        calls.append(("ssh", tuple(argv)))
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(ssh_module, "Lium", _Lium)
    monkeypatch.setattr(ssh_actions.subprocess, "run", _run)
    _patch_probe(monkeypatch, calls, probe)
    return CliRunner().invoke(cli, ["ssh", "eager-wolf-aa"]), calls


def test_ssh_opens_the_session_at_once_when_sshd_answers(monkeypatch):
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0)

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["probe", "ssh"]


def test_ssh_waits_through_a_refused_port(monkeypatch):
    answers = iter(["connection refused", "connection closed before an SSH banner", None])

    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: next(answers), returncode=0)

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["probe", "probe", "probe", "ssh"]
    assert calls[0] == ("probe", "203.0.113.10", 20022)


def test_ssh_fails_with_the_code_and_leaves_the_pod_when_ssh_never_comes_up(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "connection refused", returncode=255)

    envelope = _envelope(result)
    assert result.exit_code == EXIT_SSH_ERROR
    assert ("rm", "pod-1") not in calls
    assert calls[-1][0] == "ssh"   # still tried once after the wait ran out
    assert envelope["error"]["code"] == "ssh_connection_failed"
    assert "is RUNNING and billing, but SSH did not answer within 60s" in envelope["error"]["message"]
    assert "'lium rm eager-wolf-aa'" in envelope["error"]["message"]
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["ssh_port_answered"] is False


def test_ssh_keeps_ssh_failed_when_the_port_answered(monkeypatch):
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=255)

    assert result.exit_code == EXIT_SSH_ERROR
    assert "SSH connection to 'eager-wolf-aa' failed" in _flat(result.output)


def test_ssh_remote_exit_status_is_not_a_failure_after_the_wait(monkeypatch):
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "connection refused", returncode=3)

    assert result.exit_code == 0, result.output


def test_ssh_does_not_probe_an_ssh_cmd_it_refuses(monkeypatch):
    result, calls = _run_ssh(
        monkeypatch, probe=lambda h, p: None, returncode=0,
        pod=_pod("ssh root@203.0.113.10 -p 20022; touch /tmp/pwned"),
    )

    assert result.exit_code == EXIT_SSH_ERROR
    assert calls == []
