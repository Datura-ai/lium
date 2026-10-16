"""One OpenSSH connection per pod, shared by every ``lium exec`` in the next minutes.

Each ``lium exec`` is a new process, so a connection held in memory (``Lium``'s kept
connections) dies with it. An OpenSSH control master outlives the process: the first
command to a pod opens it (``ssh -M -N -f``, ``ControlPersist``), and every later command
runs as a new channel on it — no TCP handshake, key exchange or authentication. The master
closes itself after ``LIUM_SSH_PERSIST`` seconds (default 600) without a command.

The control socket lives in ``~/.lium/ssh-mux/`` (``0700``), one per pod id, ssh key, host-key policy and ssh
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
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .client import DEFAULT_MAX_OUTPUT_BYTES, ssh_insecure
from .exceptions import LiumError, LiumHostKeyError, LiumSSHError, OutputLimitExceeded
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
    directory.parent.mkdir(parents=True, exist_ok=True)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass  # already there: the checks below decide whether it is usable
    # Open without following a symlink another user planted at the predictable fallback path, then check and chmod the
    # opened directory itself.
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise LiumError(f"{directory} is not a plain directory; set {PERSIST_ENV}=0 or remove it") from exc
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise LiumError(f"{directory} belongs to another user; set {PERSIST_ENV}=0 or remove it")
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


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


def _key_path(lium: Any) -> Optional[str]:
    key_path = getattr(lium.config, "ssh_key_path", None)
    return str(Path(key_path).expanduser()) if key_path else None


def _pod_prefix(pod: PodInfo) -> str:
    return hashlib.sha256(f"{pod.id}\0{pod.ssh_cmd}".encode()).hexdigest()[:12]


def control_path(lium: Any, pod: PodInfo) -> Path:
    # the host-key policy and key are part of the name: a master opened under LIUM_SSH_INSECURE (or another key)
    # is never reused once they change; the pod part comes first so stop() finds every variant
    variant = hashlib.sha256(f"{_key_path(lium)}\0{ssh_insecure()}".encode()).hexdigest()[:12]
    return socket_dir() / f"{_pod_prefix(pod)}{variant}"


class ControlMaster:
    """The control master of one pod: check it, start it, run a command over it, stop it."""

    def __init__(self, lium: Any, pod: PodInfo):
        from .client import openssh_host_key_options, ssh_target

        user, host, port = ssh_target(pod.ssh_cmd)
        self.pod = pod
        self.path = control_path(lium, pod)
        self.destination = f"{user}@{host}"
        # -F /dev/null: the renter's ~/.ssh/config (SendEnv, SetEnv, ...) must not reach a pod; every option is set here
        self.base = ["ssh", "-F", "/dev/null", "-p", str(port), "-o", f'ControlPath="{self.path}"', "-o", "BatchMode=yes",
                     "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes"]
        key_path = _key_path(lium)
        if key_path:
            self.base += ["-i", key_path, "-o", "IdentitiesOnly=yes"]
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
        """Connect and leave the master in the background, unless another process just did. Raises when ssh cannot connect."""
        import fcntl   # POSIX only, like the master itself (available() is False on Windows)

        ensure_socket_dir(self.path.parent)
        # Two `lium exec` processes reaching a pod at once must not both start a master: the loser's ssh would find
        # the socket taken, drop multiplexing and stay up as a plain connection nothing ever closes. One starter at a
        # time per pod; the next one finds the master up and uses it.
        with open(f"{self.path}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not self.alive():
                self._start_locked()

    def _start_locked(self) -> None:
        from .client import _SSH_INSECURE_ENV

        try:
            self.path.unlink()   # not alive: a dead master's socket, or one that is not ours; ssh -M would refuse it
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
        # the pin notice is reported whether or not the connection then succeeded: a failed login still pinned the key
        notices = [line for line in message.splitlines() if "Permanently added" in line]
        for line in notices:
            warnings.warn(line, stacklevel=3)   # first-use pin, or the unverified accept under LIUM_SSH_INSECURE
        if started.returncode == 0:
            if ssh_insecure() and not notices:   # LogLevel QUIET hides ssh's own line; the opt-out is never silent
                warnings.warn(f"{self.destination}: {_SSH_INSECURE_ENV}=1 disabled host key verification", stacklevel=3)
            return
        name = self.pod.name or self.pod.huid
        if any(marker in message for marker in _HOST_KEY_CHANGED):
            raise LiumHostKeyError(
                f"Host key for pod {name} ({self.destination}) changed: the container, and its key, were replaced "
                f"(a reboot or template change), or the connection is being intercepted. If you trust the new key, "
                f"delete the pod's file under ~/.lium/known_hosts/ and reconnect; LIUM_SSH_INSECURE=1 disables pinning."
            )
        detail = message.splitlines()[-1] if message else f"ssh exited {started.returncode}"
        raise LiumSSHError(f"SSH to pod {name} ({self.destination}) failed: {detail}")

    def ensure(self) -> None:
        if not self.alive():
            self.start()

    def run(self, command: str, stdin: bytes = b"", max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> Dict[str, Any]:
        """Run ``command`` on the pod over the master; the same dict and output limit as :meth:`Lium.exec`."""
        argv = self._argv("-o", "ControlMaster=no", "-o", "LogLevel=ERROR", "-T", command=command)
        code, out, err, over = _capture(argv, stdin, max_output_bytes)
        if over:
            raise OutputLimitExceeded(
                f"Output of the command on pod {self.pod.name or self.pod.huid} is more than {max_output_bytes} bytes"
            )
        return {
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "exit_code": code,
            "success": code == 0,
        }

    def stop(self) -> None:
        """Close the pod's masters under every key and host-key policy, not only this client's: an API-only client
        (no saved key) must still close the one ``lium exec`` opened with the saved key."""
        own = f'ControlPath="{self.path}"'
        for path in socket_dir().glob(_pod_prefix(self.pod) + "?" * 12):
            if socket_is_ours(path):
                argv = [f'ControlPath="{path}"' if a == own else a for a in self._argv("-O", "exit")]
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=10)


def _capture(argv: List[str], stdin: bytes, max_output_bytes: int) -> Tuple[int, bytes, bytes, bool]:
    """Run ``argv`` and read stdout and stderr as they arrive; once both together pass ``max_output_bytes`` the
    ssh client is killed (closing the command's channel; the master stays up) and ``over`` is True."""
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    total, over, lock = [0], threading.Event(), threading.Lock()

    def drain(pipe) -> bytearray:
        buf = bytearray()
        while chunk := pipe.read1(65536):  # read1 returns what has arrived, so a slow trickle is counted too
            with lock:
                total[0] += len(chunk)
                if total[0] > max_output_bytes:
                    over.set()
                    proc.kill()
                    return buf
            buf += chunk
        return buf

    with ThreadPoolExecutor(max_workers=2) as readers:
        out, err = readers.submit(drain, proc.stdout), readers.submit(drain, proc.stderr)
        try:
            proc.stdin.write(stdin)
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the command exited, or was killed at the limit, before reading its stdin
        out_bytes, err_bytes = bytes(out.result()), bytes(err.result())
    code = proc.wait()
    proc.stdout.close()
    proc.stderr.close()
    return code, out_bytes, err_bytes, over.is_set()


def has_live_master(lium: Any, pod: PodInfo) -> bool:
    """Whether a master to ``pod`` is up. A live master is a connection the pod accepted, so the pod is there."""
    try:
        return ControlMaster(lium, pod).alive()
    except (ValueError, OSError, LiumError, subprocess.SubprocessError):
        return False


def exec_over_master(lium: Any, pod: PodInfo, *, command: str, env: Optional[Dict[str, str]] = None,
                     max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> Dict[str, Any]:
    """:meth:`Lium.exec` over the pod's control master (started when there is none).

    ``env`` travels over stdin exactly as :meth:`Lium.exec` sends it, so no value reaches
    the pod's argv. Raises ``ValueError`` for a bad ``env`` name before anything is sent,
    ``LiumHostKeyError``/``LiumError`` when ssh cannot connect, ``OutputLimitExceeded`` past ``max_output_bytes``.
    """
    exports = lium._env_exports(env) if env else ""
    if exports:
        command = f"{lium._ENV_FROM_STDIN} && {command}"
    master = ControlMaster(lium, pod)
    master.ensure()
    result = master.run(command, stdin=exports.encode("utf-8"), max_output_bytes=max_output_bytes)
    if result["exit_code"] == 255 and not master.alive():
        # 255 is also a command's own status; with the master gone it is ssh's: the connection dropped
        lines = result["stderr"].strip().splitlines()
        # keep what the command printed before the drop; the caller prints it, then the error
        result["error"] = f"SSH connection to pod {pod.name or pod.huid} was lost" + (f": {lines[-1]}" if lines else "")
    return result


def exec_all_over_masters(lium: Any, pods: List[PodInfo], *, command: str, env: Optional[Dict[str, str]] = None,
                          max_workers: int = 10, max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES) -> List[Dict[str, Any]]:
    """:meth:`Lium.exec_all` over control masters: one result per pod, ``{"pod", "error", "success": False}`` on failure."""
    if env:
        lium._env_exports(env)

    def one(pod: PodInfo) -> Dict[str, Any]:
        try:
            result = exec_over_master(lium, pod, command=command, env=env, max_output_bytes=max_output_bytes)
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
