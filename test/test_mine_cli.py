"""`lium mine` bring-up: a failed start explains itself, and only --register recreates a running executor (DAH-4375)."""

from __future__ import annotations

import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from lium.cli.commands import mine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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
        mine._start_executor(tmp_path, wait_secs=5, recreate=True)
    msg = str(exc.value)
    assert "timed out after 5s" in msg
    assert "Restarting (1)" in msg and "Created" in msg
    assert "address already in use" in msg and "exited with code 1" in msg
    assert "no such service" not in msg
    # the old container ID is read first, so the wait can tell the new container from it
    assert calls[:2] == ["docker compose -f docker-compose.app.yml ps -q executor", "docker compose up -d --force-recreate"]
    # each compose file is asked for the service it declares (`executor` lives in docker-compose.app.yml)
    assert [c for c in calls if "logs" in c] == [
        "docker compose logs --no-color --tail 30 executor-runner",
        "docker compose -f docker-compose.app.yml logs --no-color --tail 30 executor",
    ]
    assert [c for c in calls if c.endswith("ps -a")] == ["docker compose ps -a", "docker compose -f docker-compose.app.yml ps -a"]


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


RUNNER = "daturaai/compute-subnet-executor-runner:latest"
EXECUTOR = "daturaai/compute-subnet-executor@sha256:aa"
BAKED_COMPOSE = f"services:\n  executor:\n    image: {EXECUTOR}\n  autoheal:\n    image: willfarrell/autoheal\n"
CURRENT_NODE_IMAGES = {RUNNER, "daturaai/lium-watchtower:1.1.1", EXECUTOR, "willfarrell/autoheal", mine.PREFLIGHT_IMAGE}


def _disk(monkeypatch, free_bytes: float, available_images: set = frozenset()) -> list:
    calls = []

    def fake_run(cmd, check=True, capture=True, cwd=None):
        calls.append(cmd)
        if "DockerRootDir" in cmd:
            return "/var/lib/docker\n", ""
        if "config --images" in cmd:
            # the unrendered app template is never asked: it needs EXECUTOR_IMAGE_SHA256, which only the runner sets
            assert "app.yml" not in cmd
            return f"{RUNNER}\ndaturaai/lium-watchtower:1.1.1\n", ""
        if cmd.startswith("docker run"):
            # a real docker run would pull a missing image; the check must never get here without the runner local
            assert RUNNER in available_images
            assert "--pull" not in cmd  # Docker before 20.10 has no --pull on docker run
            return BAKED_COMPOSE, ""
        if cmd.split()[-1].strip("'") not in available_images:  # docker image inspect <ref>
            raise RuntimeError(f"Command failed (1): {cmd}")
        return "[]", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.shutil, "disk_usage", lambda path: SimpleNamespace(free=free_bytes))
    return calls


# 140e9 bytes is 130.4 GiB: under the 140 GiB the validator's GiB floor plus the image reserve needs; a stale
# executor image or a missing runner image leaves the current images to pull


def test_start_executor_without_a_register_token_does_not_recreate_a_running_executor(monkeypatch, tmp_path: Path) -> None:
    calls: list[str] = []

    def fake_run(cmd, check=True, capture=True, cwd=None):
        calls.append(cmd)
        if "ps -q executor" in cmd:
            return "abc123\n", ""
        if cmd.startswith("docker inspect"):
            return "healthy\n", ""
        return "", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.time, "sleep", lambda s: None)

    mine._start_executor(tmp_path)

    assert calls[0] == "docker compose up -d"
