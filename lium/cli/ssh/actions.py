import socket
import subprocess
import time
from typing import Callable, Optional

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo
from lium.sdk.client import ssh_target
from lium.cli import ui


class SshAction:
    """Execute SSH connection."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute SSH to pod."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]

        # The argument list comes from the pod's user, host and port, never from the
        # API's string as-is, and runs without a shell; the pod's host key is pinned.
        try:
            ssh_argv = lium.ssh_argv(pod)
        except ValueError as e:
            return ActionResult(ok=False, data={}, error=f"Pod '{pod.huid}': {e}")
        try:
            result = subprocess.run(ssh_argv, check=False)
        except KeyboardInterrupt:
            # Ctrl+C out of a session is the user's own doing, not a lium failure.
            ui.warning("\nSSH session interrupted")
            return ActionResult(ok=True, data={})

        # A non-zero remote shell is the remote's business; 255 is ssh itself.
        return ActionResult(ok=True, data={"exit_code": result.returncode})


# A pod reports RUNNING before an sshd the image starts itself is listening, and the
# node's port forward accepts TCP meanwhile and then closes it: only the server's
# "SSH-" identification line says ssh can connect. Lium.wait_for_port can't tell:
# its probe itself runs over SSH.
SSH_READY_SECONDS = 60
SSH_READY_INTERVAL = 3
SSH_PROBE_TIMEOUT = 5
_SSH_BANNER_MAX_BYTES = 4096

def host_port(host: str, port: int) -> str:
    """``host:port``, with an IPv6 address in brackets."""
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def ssh_banner_problem(host: str, port: int, timeout: float = SSH_PROBE_TIMEOUT) -> Optional[str]:
    """None when ``host:port`` sends an SSH identification line, else what it did instead.

    RFC 4253 lets a server send other lines before ``SSH-``, so up to 4 KiB is read.
    ``timeout`` bounds the whole probe, so a peer trickling bytes can't stretch it.
    """
    buffer = b""
    deadline = time.monotonic() + timeout
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            while len(buffer) < _SSH_BANNER_MAX_BYTES:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise socket.timeout()
                sock.settimeout(remaining)
                chunk = sock.recv(_SSH_BANNER_MAX_BYTES - len(buffer))
                if not chunk:
                    break
                buffer += chunk
                if any(line.startswith(b"SSH-") for line in buffer.split(b"\n")):
                    return None
    except socket.timeout:
        return "no SSH banner" if buffer else "no answer"
    except OSError as exc:
        return (exc.strerror or str(exc)).lower()
    if not buffer:
        return "connection closed before an SSH banner"
    return "not an SSH server"


class WaitForSSHAction:
    """Wait until the pod's SSH port answers with an SSH banner, for up to ``wait_seconds``.

    ``ok`` False means the port never answered as an SSH server in that time; the
    pod is left as it is, and ``data["last_problem"]`` says what the port did.
    Raises ``ValueError`` when the pod's ``ssh_cmd`` is not one ssh would be given.
    """

    def execute(self, ctx: dict) -> ActionResult:
        pod: PodInfo = ctx["pod"]
        wait_seconds: float = ctx.get("wait_seconds", SSH_READY_SECONDS)
        probe: Callable[[str, int], Optional[str]] = ctx.get("probe", ssh_banner_problem)
        sleep = ctx.get("sleep", time.sleep)
        clock = ctx.get("clock", time.monotonic)

        _user, host, port = ssh_target(pod.ssh_cmd)
        deadline = clock() + wait_seconds
        attempts = 0
        while True:
            attempts += 1
            problem = probe(host, port)
            data = {"host": host, "port": port, "attempts": attempts, "wait_seconds": wait_seconds}
            if problem is None:
                return ActionResult(ok=True, data=data)
            if clock() + SSH_READY_INTERVAL > deadline:
                return ActionResult(
                    ok=False,
                    data={**data, "last_problem": problem},
                    error=f"{host_port(host, port)} gave no SSH banner within {wait_seconds:g}s ({problem})",
                )
            sleep(SSH_READY_INTERVAL)
