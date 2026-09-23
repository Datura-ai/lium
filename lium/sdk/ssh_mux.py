"""One OpenSSH connection per pod, shared by every ``lium exec`` in the next minutes.

Each ``lium exec`` is a new process, so a connection held in memory (``Lium``'s kept
connections) dies with it. An OpenSSH control master outlives the process: the first
command to a pod opens it (``ssh -M -N -f``, ``ControlPersist``), and every later command
runs as a new channel on it — no TCP handshake, key exchange or authentication. The master
closes itself after ``LIUM_SSH_PERSIST`` seconds (default 600) without a command.

The control socket lives in ``~/.lium/ssh-mux/`` (``0700``), one per pod id and ssh
address, so a new pod on a recycled address never reaches an old pod's connection. Host
keys are checked exactly as :meth:`Lium.ssh_argv` checks them (pinned per pod).

Not used on Windows (its OpenSSH has no control master), when ``ssh`` is not on PATH, or
with ``LIUM_SSH_PERSIST=0``; the caller then runs the command over paramiko as before.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .exceptions import LiumError, LiumHostKeyError
from .models import PodInfo

PERSIST_ENV = "LIUM_SSH_PERSIST"
DEFAULT_PERSIST_SECONDS = 600
# A unix socket path is limited to 104 bytes on macOS (108 on Linux); ssh adds a 17-character
# suffix while it creates the socket, and the name here is 24 characters.
_MAX_SOCKET_DIR_LEN = 104 - 17 - 24 - 2
_HOST_KEY_CHANGED = ("REMOTE HOST IDENTIFICATION HAS CHANGED", "Host key verification failed")


def persist_seconds() -> int:
    """``LIUM_SSH_PERSIST``: seconds an idle master stays up; 0 turns the master off."""
    raw = os.getenv(PERSIST_ENV, "").strip()
    if not raw:
        return DEFAULT_PERSIST_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_PERSIST_SECONDS


def available() -> bool:
    """Whether commands can go through a control master here."""
    return os.name != "nt" and persist_seconds() > 0 and shutil.which("ssh") is not None


def socket_dir() -> Path:
    """``~/.lium/ssh-mux``, or a directory under the temp dir when that path is too long for a socket."""
    home_dir = Path.home() / ".lium" / "ssh-mux"
    if len(str(home_dir)) <= _MAX_SOCKET_DIR_LEN:
        return home_dir
    return Path(tempfile.gettempdir()) / f"lium-ssh-mux-{os.getuid()}"


def ensure_socket_dir(directory: Path) -> None:
    """Create ``directory`` ``0700``; refuse one another user owns (a shared temp dir)."""
    directory.mkdir(parents=True, exist_ok=True)
    if directory.stat().st_uid != os.getuid():
        raise LiumError(f"{directory} belongs to another user; set {PERSIST_ENV}=0 or remove it")
    os.chmod(directory, 0o700)


def socket_is_ours(path: Path) -> bool:
    """Whether ``path`` and its directory belong to this user and the directory is closed to others.

    The OpenSSH mux client never checks who owns the master it talks to, so a socket another
    local user planted (in the temp-dir fallback) would receive the commands and ``-e`` values.
    """
    try:
        directory, sock = path.parent.lstat(), path.lstat()
    except OSError:
        return False
    uid = os.getuid()
    return (
        stat.S_ISDIR(directory.st_mode) and directory.st_uid == uid and not directory.st_mode & 0o077
        and sock.st_uid == uid
    )


def control_path(pod: PodInfo) -> Path:
    digest = hashlib.sha256(f"{pod.id}\0{pod.ssh_cmd}".encode()).hexdigest()[:24]
    return socket_dir() / digest


class ControlMaster:
    """The control master of one pod: check it, start it, run a command over it, stop it."""

    def __init__(self, lium: Any, pod: PodInfo):
        from .client import openssh_host_key_options, ssh_target

        user, host, port = ssh_target(pod.ssh_cmd)
        self.pod = pod
        self.path = control_path(pod)
        self.destination = f"{user}@{host}"
        self.base = ["ssh", "-p", str(port), "-o", f"ControlPath={self.path}", "-o", "BatchMode=yes"]
        key_path = getattr(lium.config, "ssh_key_path", None)
        if key_path:
            self.base += ["-i", str(Path(key_path).expanduser()), "-o", "IdentitiesOnly=yes"]
        self.base += openssh_host_key_options(pod, create_pin_file=False)   # ssh writes the pin on first connect

    def _argv(self, *options: str, command: Optional[str] = None) -> List[str]:
        # `--` ends ssh's options: without it ssh parses a command that starts with "-" as more of them
        return [*self.base, *options, "--", self.destination, *([command] if command is not None else [])]

    def alive(self) -> bool:
        if not socket_is_ours(self.path):
            return False
        check = subprocess.run(self._argv("-O", "check"), stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return check.returncode == 0

    def start(self) -> None:
        """Connect and leave the master in the background. Raises when ssh cannot connect."""
        ensure_socket_dir(self.path.parent)
        try:
            self.path.unlink()   # a dead master's socket, or one that is not ours: ssh -M would refuse the path
        except FileNotFoundError:
            pass  # no socket left behind: nothing stands in ssh -M's way
        # stderr goes to a file: the backgrounded master keeps it open, and a pipe would never see EOF
        with tempfile.TemporaryFile() as err:
            started = subprocess.run(
                self._argv(
                    "-o", "ControlMaster=yes", "-o", f"ControlPersist={persist_seconds()}",
                    "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4",
                    "-N", "-f",
                ),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, timeout=120,
            )
            err.seek(0)
            message = err.read().decode("utf-8", errors="replace").strip()
        if started.returncode == 0:
            return
        name = self.pod.name or self.pod.huid
        if any(marker in message for marker in _HOST_KEY_CHANGED):
            raise LiumHostKeyError(
                f"Host key for pod {name} ({self.destination}) changed: the container, and its key, were replaced "
                f"(a reboot or template change), or the connection is being intercepted. If you trust the new key, "
                f"delete the pod's file under ~/.lium/known_hosts/ and reconnect; LIUM_SSH_INSECURE=1 disables pinning."
            )
        detail = message.splitlines()[-1] if message else f"ssh exited {started.returncode}"
        raise LiumError(f"SSH to pod {name} ({self.destination}) failed: {detail}")

    def ensure(self) -> None:
        if not self.alive():
            self.start()

    def run(self, command: str, stdin: bytes = b"") -> Dict[str, Any]:
        """Run ``command`` on the pod over the master; the same dict as :meth:`Lium.exec`."""
        done = subprocess.run(self._argv("-o", "ControlMaster=no", "-o", "LogLevel=ERROR", "-T", command=command),
                              input=stdin, capture_output=True)
        return {
            "stdout": done.stdout.decode("utf-8", errors="replace"),
            "stderr": done.stderr.decode("utf-8", errors="replace"),
            "exit_code": done.returncode,
            "success": done.returncode == 0,
        }

    def stop(self) -> None:
        if socket_is_ours(self.path):
            subprocess.run(self._argv("-O", "exit"), stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)


def has_live_master(lium: Any, pod: PodInfo) -> bool:
    """Whether a master to ``pod`` is up. A live master is a connection the pod accepted, so the pod is there."""
    try:
        return ControlMaster(lium, pod).alive()
    except (ValueError, OSError, LiumError, subprocess.SubprocessError):
        return False


def exec_over_master(lium: Any, pod: PodInfo, *, command: str, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """:meth:`Lium.exec` over the pod's control master (started when there is none).

    ``env`` travels over stdin exactly as :meth:`Lium.exec` sends it, so no value reaches
    the pod's argv. Raises ``ValueError`` for a bad ``env`` name before anything is sent,
    ``LiumHostKeyError``/``LiumError`` when ssh cannot connect.
    """
    exports = lium._env_exports(env) if env else ""
    if exports:
        command = f"{lium._ENV_FROM_STDIN} && {command}"
    master = ControlMaster(lium, pod)
    master.ensure()
    result = master.run(command, stdin=exports.encode("utf-8"))
    if result["exit_code"] == 255 and not master.alive():
        # 255 is also a command's own status; with the master gone it is ssh's: the connection dropped
        lines = result["stderr"].strip().splitlines()
        raise LiumError(f"SSH connection to pod {pod.name or pod.huid} was lost" + (f": {lines[-1]}" if lines else ""))
    return result


def exec_all_over_masters(lium: Any, pods: List[PodInfo], *, command: str, env: Optional[Dict[str, str]] = None,
                          max_workers: int = 10) -> List[Dict[str, Any]]:
    """:meth:`Lium.exec_all` over control masters: one result per pod, ``{"pod", "error", "success": False}`` on failure."""
    if env:
        lium._env_exports(env)

    def one(pod: PodInfo) -> Dict[str, Any]:
        try:
            result = exec_over_master(lium, pod, command=command, env=env)
            result["pod"] = pod.id
            return result
        except Exception as e:  # noqa: BLE001 — one unreachable pod is one failed entry, as in Lium.exec_all
            return {"pod": pod.id, "error": str(e), "success": False}

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pods)))) as pool:
        return list(pool.map(one, pods))


def stop(lium: Any, pod: PodInfo) -> None:
    """Close the pod's master, if one is up (its container is going away or being replaced)."""
    try:
        ControlMaster(lium, pod).stop()
    except (ValueError, OSError, LiumError, subprocess.SubprocessError):
        pass  # best effort: the pod is going away, and a master left behind exits after its ControlPersist idle limit
