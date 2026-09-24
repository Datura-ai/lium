"""`lium up` and `lium ssh` wait for the pod's SSH banner before opening the session.

A pod reports RUNNING before an sshd the image starts itself is listening; the node's port
forward accepts the TCP connection meanwhile and closes it. Both commands made one ssh attempt,
so that start-up race failed. They now poll the SSH port for up to 60 s for the server's `SSH-`
line, then open the session (still tried once when the wait runs out). The pod is never removed.
"""

import json
import socket
import threading
import shutil
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.ssh import actions as ssh_actions
from lium.cli.ssh import command as ssh_module
from lium.cli.up import command as up_module
from lium.cli.utils import EXIT_GENERAL_ERROR, EXIT_SSH_ERROR
from lium.sdk import PodInfo


def _pod(ssh_cmd="ssh root@203.0.113.10 -p 20022", created_at="2026-09-05T10:00:00Z",
         updated_at="2026-09-05T10:00:00Z") -> PodInfo:
    return PodInfo(
        id="pod-1", name="train", status="RUNNING", huid="eager-wolf-aa",
        ssh_cmd=ssh_cmd, ports={"22": 20022}, created_at=created_at,
        updated_at=updated_at, executor=None, template={}, removal_scheduled_at=None,
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


def test_a_trickling_peer_cannot_stretch_one_probe(server):
    """The timeout bounds the whole probe, not each recv."""
    release = threading.Event()

    def trickle(conn):
        while not release.is_set():
            try:
                conn.sendall(b"x")
            except OSError:
                return
            release.wait(0.05)

    port = server(trickle)
    started = time.monotonic()
    try:
        problem = ssh_actions.ssh_banner_problem("127.0.0.1", port, timeout=0.3)
    finally:
        release.set()

    assert problem == "no SSH banner"
    assert time.monotonic() - started < 1.0


def test_an_ipv6_address_is_shown_in_brackets():
    assert ssh_actions.host_port("2001:db8::1", 20022) == "[2001:db8::1]:20022"
    assert ssh_actions.host_port("203.0.113.10", 20022) == "203.0.113.10:20022"

    result = _wait(lambda h, p: "connection refused", _Clock(), pod=_pod("ssh root@2001:db8::1 -p 20022"))

    assert result.error.startswith("[2001:db8::1]:20022 gave no SSH banner")


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


def test_a_trickling_peer_uses_the_probe_deadline():
    """No real socket: each recv returns a byte and the clock moves 0.1 s."""
    now = {"t": 0.0}

    class _Sock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, value):
            assert value > 0

        def recv(self, n):
            now["t"] += 0.1
            return b"x"

    import unittest.mock as mock
    with mock.patch.object(ssh_actions.socket, "create_connection", lambda *a, **k: _Sock()), \
            mock.patch.object(ssh_actions.time, "monotonic", lambda: now["t"]):
        assert ssh_actions.ssh_banner_problem("203.0.113.10", 20022, timeout=0.5) == "no SSH banner"

    assert now["t"] <= 0.6


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

def _ago(minutes: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _patch_probe(monkeypatch, calls, probe, route=None):
    """Route the real wait through ``probe`` on a fake clock; ``ssh -G`` answers ``route``."""
    clock = _Clock()
    monkeypatch.setattr(ssh_module, "ssh_route_skip_reason", lambda argv, host: route)

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

def _run_up(monkeypatch, *, probe, ssh_connects, json_output=False, route=None):
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
    _patch_probe(monkeypatch, calls, probe, route)

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


def test_up_key_failure_envelope_says_the_port_answered(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=False)

    envelope = _envelope(result)
    assert envelope["error"]["code"] == "ssh_connection_failed"
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["ssh_port_answered"] is True


def test_up_json_does_not_probe_ssh(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=True, json_output=True)

    assert result.exit_code == 0, result.output
    assert not [c for c in calls if c[0] in ("probe", "ssh")]


# --- lium ssh -----------------------------------------------------------------------------------

def _run_ssh(monkeypatch, *, probe, returncode, pod=None, route=None):
    """`lium ssh eager-wolf-aa` on a pod created a minute ago, against a fake pod list; returns (result, calls)."""
    calls: list = []
    target = pod or _pod(created_at=_ago(1), updated_at=_ago(1))

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
    _patch_probe(monkeypatch, calls, probe, route)
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


def test_ssh_keeps_ssh_failed_and_leaves_the_pod_when_ssh_never_comes_up(monkeypatch):
    """`lium ssh`'s code for ssh's own failure stays `ssh_failed`; only `data` grows."""
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "connection refused", returncode=255)

    envelope = _envelope(result)
    assert result.exit_code == EXIT_SSH_ERROR
    assert ("rm", "pod-1") not in calls
    assert calls[-1][0] == "ssh"   # still tried once after the wait ran out
    assert envelope["error"]["code"] == "ssh_failed"
    assert envelope["error"]["message"] == "SSH connection to 'eager-wolf-aa' failed"
    assert envelope["data"]["pod_id"] == "pod-1"
    assert envelope["data"]["pod_name"] == "train"
    assert envelope["data"]["ssh_port_answered"] is False
    assert envelope["data"]["ssh_wait"]["last_problem"] == "connection refused"


def test_ssh_keeps_ssh_failed_when_the_port_answered(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=255)

    envelope = _envelope(result)
    assert result.exit_code == EXIT_SSH_ERROR
    assert envelope["error"]["code"] == "ssh_failed"
    assert envelope["error"]["message"] == "SSH connection to 'eager-wolf-aa' failed"
    assert envelope["data"]["ssh_port_answered"] is True


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


# --- when the wait is skipped -------------------------------------------------------------------

def _resolved(monkeypatch, stdout, returncode=0):
    """``ssh -G`` answering ``stdout``; returns the argv lists it was given."""
    seen = []

    def _run(argv, **kwargs):
        seen.append(argv)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    monkeypatch.setattr(ssh_module.subprocess, "run", _run)
    return seen


_ARGV = ["ssh", "-i", "/k/id", "-p", "20022", "root@203.0.113.10"]
_DIRECT = "user root\nhostname 203.0.113.10\nport 20022\nproxycommand none\n"


def test_route_check_runs_ssh_g_on_the_same_arguments(monkeypatch):
    seen = _resolved(monkeypatch, _DIRECT)

    assert ssh_module.ssh_route_skip_reason(_ARGV, "203.0.113.10") is None
    assert seen == [["ssh", "-G", "-i", "/k/id", "-p", "20022", "root@203.0.113.10"]]


@pytest.mark.parametrize(
    "stdout, reason",
    [
        (_DIRECT + "proxyjump bastion\n", "ssh config sets ProxyJump"),
        ("hostname 203.0.113.10\nproxycommand nc -X 5 -x proxy:1080 %h %p\n", "ssh config sets ProxyCommand"),
        ("hostname pod-gw.internal\nport 20022\n", "ssh config sends 203.0.113.10 to pod-gw.internal"),
    ],
)
def test_route_check_names_a_route_the_probe_would_miss(monkeypatch, stdout, reason):
    _resolved(monkeypatch, stdout)

    assert ssh_module.ssh_route_skip_reason(_ARGV, "203.0.113.10") == reason


def test_route_check_falls_back_to_waiting_when_ssh_g_fails(monkeypatch):
    _resolved(monkeypatch, "", returncode=255)

    assert ssh_module.ssh_route_skip_reason(_ARGV, "203.0.113.10") is None


@pytest.mark.skipif(not shutil.which("ssh"), reason="no ssh client")
def test_route_check_reads_a_real_ssh_config(tmp_path):
    config = tmp_path / "config"
    config.write_text("Host 203.0.113.10\n  ProxyJump bastion.example\n")
    argv = ["ssh", "-F", str(config), "-p", "20022", "root@203.0.113.10"]

    assert ssh_module.ssh_route_skip_reason(argv, "203.0.113.10") == "ssh config sets ProxyJump"
    config.write_text("")
    assert ssh_module.ssh_route_skip_reason(argv, "203.0.113.10") is None


def test_up_skips_the_wait_behind_a_proxy(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=False, route="ssh config sets ProxyJump")

    envelope = _envelope(result)
    assert [c[0] for c in calls] == ["ssh"]
    assert envelope["error"]["code"] == "ssh_connection_failed"
    assert envelope["data"]["ssh_wait"] == {"skipped": "ssh config sets ProxyJump"}
    assert "ssh_port_answered" not in envelope["data"]


def test_ssh_skips_the_wait_behind_a_proxy(monkeypatch):
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0, route="ssh config sets ProxyJump")

    assert result.exit_code == 0, result.output
    assert [c[0] for c in calls] == ["ssh"]


def test_ssh_skips_the_wait_for_a_pod_unchanged_for_over_ten_minutes(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    pod = _pod(created_at=_ago(180), updated_at=_ago(11))

    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "connection refused", returncode=255, pod=pod)

    envelope = _envelope(result)
    assert [c[0] for c in calls] == ["ssh"]
    assert envelope["error"]["code"] == "ssh_failed"
    assert envelope["data"]["ssh_wait"] == {"skipped": "pod unchanged for 11 min"}


def test_ssh_waits_for_a_pod_updated_in_the_last_ten_minutes(monkeypatch):
    """An old pod that just changed (a restart) is a fresh sshd start: the later stamp counts."""
    pod = _pod(created_at=_ago(180), updated_at=_ago(2))

    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0, pod=pod)

    assert [c[0] for c in calls] == ["probe", "ssh"]


@pytest.mark.parametrize("stamp", ["", "not a time"])
def test_ssh_waits_when_the_pod_has_no_usable_timestamp(monkeypatch, stamp):
    pod = _pod(created_at=stamp, updated_at=stamp)

    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0, pod=pod)

    assert [c[0] for c in calls] == ["probe", "ssh"]


def test_up_waits_whatever_the_pod_age(monkeypatch):
    """`lium up` just rented the pod: its age is never a reason to skip."""
    result, calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=True)

    assert [c[0] for c in calls] == ["probe", "ssh"]


# --- the try-once after a wait that saw no banner -----------------------------------------------

def _ssh_argv(calls):
    return next(list(c[1]) for c in calls if c[0] == "ssh")


def test_up_try_once_has_a_connect_timeout(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=lambda h, p: "no answer", ssh_connects=False)

    argv = _ssh_argv(calls)
    assert argv[:3] == ["ssh", "-o", "ConnectTimeout=15"]
    assert argv[-1] == "root@203.0.113.10"


def test_ssh_try_once_has_a_connect_timeout(monkeypatch):
    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "no answer", returncode=255)

    argv = _ssh_argv(calls)
    assert argv[:3] == ["ssh", "-o", "ConnectTimeout=15"]
    assert argv[-1] == "root@203.0.113.10"


@pytest.mark.parametrize("route", [None, "ssh config sets ProxyJump"])
def test_a_session_after_a_banner_or_a_skipped_wait_has_no_connect_timeout(monkeypatch, route):
    _, up_calls = _run_up(monkeypatch, probe=lambda h, p: None, ssh_connects=True, route=route)
    _, ssh_calls = _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0, route=route)

    assert "ConnectTimeout=15" not in _ssh_argv(up_calls)
    assert "ConnectTimeout=15" not in _ssh_argv(ssh_calls)


# --- Ctrl-C during the wait ---------------------------------------------------------------------

def _interrupt(host, port):
    raise KeyboardInterrupt


def test_up_ctrl_c_in_the_wait_names_the_billing_pod(monkeypatch):
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    result, calls = _run_up(monkeypatch, probe=_interrupt, ssh_connects=True)

    envelope = _envelope(result)
    assert result.exit_code == EXIT_GENERAL_ERROR
    assert envelope["error"]["code"] == "ssh_wait_interrupted"
    assert "pod eager-wolf-aa is RUNNING and billing" in envelope["error"]["message"]
    assert "'lium ssh eager-wolf-aa'" in envelope["error"]["hint"]
    assert "'lium rm eager-wolf-aa'" in envelope["error"]["hint"]
    assert envelope["data"] == {"pod_id": "pod-1", "pod_name": "train"}
    assert not [c for c in calls if c[0] in ("ssh", "rm")]


def test_up_ctrl_c_in_the_wait_reads_for_a_person(monkeypatch):
    result, calls = _run_up(monkeypatch, probe=_interrupt, ssh_connects=True)

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Stopped waiting for SSH; pod eager-wolf-aa is RUNNING and billing" in _flat(result.output)
    assert "Aborted!" not in result.output


# --- IPv6 hosts through Rich markup -------------------------------------------------------------

def test_an_ipv6_host_that_reads_as_a_rich_tag_survives_the_warning(monkeypatch):
    """`[fe80::1]` is valid Rich markup; unescaped it vanished from the line."""
    pod = _pod("ssh root@fe80::1 -p 22", created_at=_ago(1), updated_at=_ago(1))

    result, calls = _run_ssh(monkeypatch, probe=lambda h, p: "connection refused", returncode=0, pod=pod)

    assert result.exit_code == 0, result.output
    assert "[fe80::1]:22 gave no SSH banner within 60s" in _flat(result.output)


def test_an_ipv6_host_that_reads_as_a_rich_tag_survives_the_spinner(monkeypatch):
    from rich.text import Text
    from lium.cli.utils import console

    labels = []
    monkeypatch.setattr(ssh_module.ui, "load", lambda message, fn: (labels.append(message), fn())[1])
    pod = _pod("ssh root@fe80::1 -p 22", created_at=_ago(1), updated_at=_ago(1))

    _run_ssh(monkeypatch, probe=lambda h, p: None, returncode=0, pod=pod)

    rendered = Text.from_markup(console.get_styled(labels[-1] + "...", "info")).plain
    assert rendered == "Waiting for SSH on [fe80::1]:22..."
