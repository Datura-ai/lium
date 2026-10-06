"""Unit tests for the `lium mine` helpers that make a failed bring-up explain itself."""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest

from lium.cli.commands import mine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_port_in_use_detects_a_listener() -> None:
    # Loopback-only listener: the wildcard probe (what compose would bind) must
    # still report the port taken, and nothing in the test listens publicly.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert mine._port_in_use(port) is True
        assert mine._port_in_use(port, host="127.0.0.1") is True
    # closed again -> free
    assert mine._port_in_use(port) is False


def test_port_in_use_is_not_fooled_by_a_privileged_port(monkeypatch) -> None:
    """An unprivileged `lium mine` cannot bind 443 (EACCES) — that is not "in use": compose binds it as root."""
    import errno

    class Denied(socket.socket):
        def bind(self, address):
            raise PermissionError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(socket, "socket", Denied)   # the function imports the socket module itself; the class is patched
    assert mine._port_in_use(443) is False

    class Taken(socket.socket):
        def bind(self, address):
            raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(socket, "socket", Taken)
    assert mine._port_in_use(443) is True


def _plain_bind_fails(port: int) -> bool:
    """e7d0770's probe: a bind without SO_REUSEADDR, which TIME_WAIT sockets refuse too."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
        except OSError:
            return True
    return False


def _leave_only_time_wait_on(port_holder: socket.socket) -> int:
    """Serve one connection and close the server side first, so the port keeps a TIME_WAIT socket and nothing else."""
    port_holder.listen(1)
    port = port_holder.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = port_holder.accept()
    served.close()          # the active closer is the side that ends in TIME_WAIT
    client.close()
    port_holder.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _plain_bind_fails(port):
        time.sleep(0.05)    # the FIN exchange on loopback is quick but not synchronous with close()
    return port


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the SO_REUSEADDR probe is Linux-only; lium mine brings up a Linux host")
def test_port_in_use_ignores_time_wait_left_by_a_stopped_listener() -> None:
    """A re-run within 60 s of `docker compose down`: the executor's ports carry only TIME_WAIT sockets. docker-proxy
    binds them fine (Go sets SO_REUSEADDR), so the pre-check must not refuse them — e7d0770's plain bind did."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # as docker-proxy's listener is
    listener.bind(("127.0.0.1", 0))
    port = _leave_only_time_wait_on(listener)
    assert _plain_bind_fails(port), "no TIME_WAIT socket on the port: the scenario did not happen, the test proves nothing"
    assert mine._port_in_use(port) is False
    assert mine._port_in_use(port, host="127.0.0.1") is False


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the SO_REUSEADDR probe is Linux-only")
def test_port_in_use_still_refuses_a_listener_that_set_reuseaddr_itself() -> None:
    # SO_REUSEADDR on the probe does not turn it permissive: a LISTEN socket that also set the option (every Go
    # listener, docker-proxy included) still refuses the bind — on the wildcard and on one interface alike
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        assert mine._port_in_use(port) is True
        assert mine._port_in_use(port, host="127.0.0.1") is True
    assert mine._port_in_use(port) is False


def test_check_ports_free_names_port_and_owner(monkeypatch) -> None:
    monkeypatch.setattr(mine, "_port_in_use", lambda port: port == 8080)
    monkeypatch.setattr(mine, "_listening_process", lambda port: "python3 pid 3641")
    with pytest.raises(Exception) as exc:
        mine._check_ports_free({"service port": 8080, "SSH port": 2200})
    msg = str(exc.value)
    assert "Port 8080 (service port)" in msg
    assert "python3 pid 3641" in msg
    assert "without --auto" in msg


def test_check_ports_free_is_skipped_when_the_executor_already_runs(monkeypatch, tmp_path: Path) -> None:
    """A re-run on a host whose executor is up: the ports are held by our own docker-proxy and
    `docker compose up -d` is a no-op, so the pre-check must not fail on them."""
    monkeypatch.setattr(mine, "_port_in_use", lambda port, host="0.0.0.0": True)
    monkeypatch.setattr(mine, "_listening_process", lambda port: "docker-proxy pid 4242")
    compose_calls: list[str] = []

    def run(cmd, check=True, capture=True, cwd=None):
        compose_calls.append(cmd)
        return ("abc123\ndef456\n", "")

    monkeypatch.setattr(mine, "_run", run)

    mine._check_ports_free({"service port": 8080, "SSH port": 2200}, tmp_path)   # no exception
    # the executor service alone, in the running state: watchtower is always up, and a
    # crash-looping executor holds no port
    assert compose_calls == ["docker compose -f docker-compose.app.yml ps -q --status running executor"]

    monkeypatch.setattr(mine, "_run", lambda cmd, check=True, capture=True, cwd=None: ("", ""))
    with pytest.raises(Exception, match="Port 8080 .* already in use"):
        mine._check_ports_free({"service port": 8080, "SSH port": 2200}, tmp_path)


def test_check_ports_free_passes_when_nothing_listens(monkeypatch) -> None:
    monkeypatch.setattr(mine, "_port_in_use", lambda port: False)
    mine._check_ports_free({"service port": _free_port(), "SSH port": _free_port()})


def test_listening_process_parses_ss_output(monkeypatch) -> None:
    ss = (
        "State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        'LISTEN 0      5            0.0.0.0:8080      0.0.0.0:*    users:(("python3",pid=3641,fd=3))\n'
        'LISTEN 0      128          0.0.0.0:22        0.0.0.0:*    users:(("sshd",pid=1,fd=3))\n'
    )
    monkeypatch.setattr(mine, "_exists", lambda cmd: True)
    monkeypatch.setattr(mine, "_run", lambda *a, **k: (ss, ""))
    assert mine._listening_process(8080) == "python3 pid 3641"
    assert mine._listening_process(2200) == ""


def test_host_ports_from_answers_skips_public_ssh_and_blanks() -> None:
    answers = {
        "external_port": "8080",
        "ssh_port": "2200",
        "ssh_public_port": "2299",  # NAT forward on the router, not bound here
        "port_range": "",
    }
    assert mine._host_ports_from_answers(answers) == {"service port": 8080, "SSH port": 2200}
    assert mine._host_ports_from_answers({"external_port": "", "ssh_port": None}) == {}


def test_start_executor_timeout_includes_compose_diagnostics(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(cmd, check=True, capture=True, cwd=None):
        calls.append(cmd)
        if cmd.startswith("docker compose up"):
            return "", ""
        if "ps -q executor" in cmd:
            return "abc123\n", ""
        if cmd.startswith("docker inspect"):
            return "unhealthy\n", ""
        if cmd == "docker compose ps -a":
            return "executor-executor-runner-1  Restarting (1)\n", ""
        if cmd == "docker compose -f docker-compose.app.yml ps -a":
            return "executor-executor-1  Created\n", ""
        if cmd == "docker compose logs --no-color --tail 30 executor-runner":
            return "failed to bind host port 0.0.0.0:8080/tcp: address already in use\n", ""
        if cmd == "docker compose -f docker-compose.app.yml logs --no-color --tail 30 executor":
            return "", "executor-executor-1 exited with code 1\n"
        if cmd.startswith("docker compose") and "logs" in cmd:
            return "", f"no such service: {cmd.split()[-1]}\n"   # what compose says for a service the file lacks
        return "", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.time, "sleep", lambda s: None)
    # Make the wait loop run exactly once, then time out.
    ticks = iter([0.0, 0.0, 10.0, 10.0])
    monkeypatch.setattr(mine.time, "time", lambda: next(ticks))

    with pytest.raises(Exception) as exc:
        mine._start_executor(tmp_path, wait_secs=5)
    msg = str(exc.value)
    assert "timed out after 5s" in msg
    assert "Restarting (1)" in msg and "Created" in msg
    assert "address already in use" in msg and "exited with code 1" in msg
    assert "no such service" not in msg
    # each compose file is asked for the service it declares (`executor` lives in docker-compose.app.yml)
    assert [c for c in calls if "logs" in c] == [
        "docker compose logs --no-color --tail 30 executor-runner",
        "docker compose -f docker-compose.app.yml logs --no-color --tail 30 executor",
    ]
    assert [c for c in calls if c.endswith("ps -a")] == ["docker compose ps -a", "docker compose -f docker-compose.app.yml ps -a"]


def test_provider_add_command_and_note() -> None:
    cmd = mine._provider_add_command(
        {"gpu_type": "NVIDIA RTX A6000", "gpu_count": 1}, "203.0.113.42", "8080"
    )
    assert cmd == (
        "lium provider node add --gpu-type 'NVIDIA RTX A6000' --gpu-count 1 "
        "--ip 203.0.113.42 --port 8080 --yes"
    )
    note = mine._registration_note()
    assert "opt-in" in note and "VALIDATION_PENDING" in note
    # every IP service failed: _get_public_ip's sentinel is not pasted into the command as three words
    cmd = mine._provider_add_command({"gpu_type": "NVIDIA RTX A6000", "gpu_count": 1}, "Unable to determine", 8080)
    assert "--ip <public IPv4> --port 8080 --yes" in cmd and "Unable" not in cmd


class _FakePreflight:
    """Stands in for the `docker run … lium-validator` process: debug log on stderr, verdict on stdout."""

    last_cmd: str | None = None

    def __init__(self, cmd, **kwargs):
        _FakePreflight.last_cmd = cmd
        self.stderr = iter(
            [
                "2026-09-06 08:00:29,011 - __main__ - DEBUG - Starting preflight validation checks...\n",
                "2026-09-06 08:00:29,011 - __main__ - DEBUG - Running check: GPU Configuration\n",
                "2026-09-06 08:00:29,046 - __main__ - DEBUG - Running check: GPU Matrix Multiplication\n",
                "2026-09-06 08:00:45,216 - __main__ - DEBUG - Running check: VerifyX (RAM/Storage/Network)\n",
            ]
        )
        self.stdout = _Out(self.verdict)
        self.returncode = 0

    verdict = '{\n  "passed": true\n}\n'

    def wait(self):
        return self.returncode


class _Out:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text


def test_validate_executor_streams_check_names_and_reads_verdict(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePreflight)
    seen: list[str] = []

    mine._validate_executor(["--gpu-max-count", "8"], on_check=seen.append)

    assert seen == ["GPU Configuration", "GPU Matrix Multiplication", "VerifyX (RAM/Storage/Network)"]
    assert _FakePreflight.last_cmd == (
        f"docker run --rm --gpus all {mine.PREFLIGHT_IMAGE} --debug --gpu-max-count 8"
    )


def test_validate_executor_reads_the_verdict_behind_debug_noise(monkeypatch) -> None:
    import subprocess

    class Noisy(_FakePreflight):
        # --debug echoes the matrix check's stdout, including raw cipher bytes, before the verdict
        verdict = (
            "processChallengeResult secret_message:17cac6d6\nRaw cipher text:\ufffd\ufffd{\ufffdJ\n"
            "Compute Capability: 8.9\n{\n  \"passed\": true\n}\n"
        )

    monkeypatch.setattr(subprocess, "Popen", Noisy)
    mine._validate_executor()  # no exception: the verdict was found


def test_validate_executor_survives_a_stdout_larger_than_the_pipe(monkeypatch) -> None:
    """A real child: 1 MiB of --debug noise on stdout before the verdict, with stderr streaming
    the check names. Reading stderr to EOF first would hang here (the child blocks on a full pipe)."""
    import subprocess
    import sys

    script = (
        "import sys; sys.stderr.write('Running check: GPU Configuration\\n'); sys.stderr.flush(); "
        "sys.stdout.write('x' * (1 << 20) + '\\n'); sys.stdout.write('{\\n  \"passed\": true\\n}\\n')"
    )
    real_popen = subprocess.Popen

    def popen(cmd, **kwargs):
        kwargs.pop("shell", None)
        return real_popen([sys.executable, "-c", script], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)
    seen: list[str] = []
    outcome: list[object] = []

    def run() -> None:
        try:
            mine._validate_executor(on_check=seen.append)   # returns: the verdict was read behind 1 MiB of noise
            outcome.append("returned")
        except Exception as exc:  # noqa: BLE001 — re-raised below on the test thread
            outcome.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout=60)   # without the drain thread this hangs forever on the full pipe: fail, do not hang
    assert outcome, "hung: _validate_executor did not return within 60 s (stdout pipe never drained)"
    if isinstance(outcome[0], Exception):
        raise outcome[0]
    assert seen == ["GPU Configuration"]


def test_validate_executor_raises_the_image_message(monkeypatch) -> None:
    import subprocess

    class Failed(_FakePreflight):
        verdict = '{"passed": false, "message": "GPU Configuration: no GPU found"}'

    monkeypatch.setattr(subprocess, "Popen", Failed)
    with pytest.raises(Exception, match="no GPU found"):
        mine._validate_executor()


def test_validate_executor_without_verdict_shows_stderr_tail(monkeypatch) -> None:
    import subprocess

    class NoVerdict(_FakePreflight):
        verdict = ""

        def __init__(self, cmd, **kwargs):
            super().__init__(cmd, **kwargs)
            self.stderr = iter(["docker: Error response from daemon: could not select device driver\n"])
            self.returncode = 125

    monkeypatch.setattr(subprocess, "Popen", NoVerdict)
    with pytest.raises(Exception) as exc:
        mine._validate_executor()
    assert "no verdict (exit 125)" in str(exc.value)
    assert "could not select device driver" in str(exc.value)


def test_start_preflight_pull_pulls_the_validation_image(monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", _FakePreflight)
    mine._start_preflight_pull()
    assert _FakePreflight.last_cmd == f"docker pull {mine.PREFLIGHT_IMAGE}"


def test_step_message_shows_the_live_detail() -> None:
    msg = mine._StepMessage("Validating node")
    assert str(msg) == "Validating node"
    msg.detail = "GPU Matrix Multiplication"
    assert str(msg) == "Validating node (GPU Matrix Multiplication)"
