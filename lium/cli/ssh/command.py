"""SSH command implementation."""

import shutil
import subprocess
from datetime import datetime, timezone
from typing import List, Optional, Tuple
import click

from lium.sdk import Lium, PodInfo
from lium.sdk.client import ssh_target
from lium.cli import ui
from lium.cli.actions import ActionResult
from lium.cli.utils import handle_errors, parse_targets, parse_timestamp
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND, EXIT_SSH_ERROR
from . import validation, parsing
from .actions import TRY_ONCE_SSH_OPTIONS, SshAction, WaitForSSHAction, host_port, with_ssh_options


# ssh(1) uses 255 for its own connection failures; anything else is the remote
# shell's own exit status, which is not a failure of the lium command.
_SSH_CONNECTION_FAILED = 255

# `lium ssh` waits for the banner only on a pod that changed less than this long ago:
# an older one is past the sshd start-up race the wait covers.
POD_SETTLED_SECONDS = 600


def get_ssh_method_and_pod(target: str) -> Tuple[List[str], PodInfo]:
    """The ssh argument list for a pod. Raises when SSH is not possible."""
    if not shutil.which("ssh"):
        raise CliFailure(
            "ssh_client_missing",
            "'ssh' command not found. Please install an SSH client.",
            EXIT_SSH_ERROR,
        )

    lium = Lium()
    all_pods = lium.ps()

    pods = parse_targets(target, all_pods)
    pod = pods[0] if pods else None

    if not pod:
        raise CliFailure("pod_not_found", f"No pods match targets: {target}", EXIT_POD_NOT_FOUND)

    if not pod.ssh_cmd:
        raise CliFailure(
            "ssh_unavailable",
            f"No SSH connection available for pod '{pod.huid}'",
            EXIT_SSH_ERROR,
        )

    # The pod's user, host and port become the argument list (no shell), with the
    # pinned host-key options; a value of any other shape is refused here.
    try:
        return lium.ssh_argv(pod), pod
    except ValueError as e:
        raise CliFailure("ssh_unavailable", f"Pod '{pod.huid}': {e}", EXIT_SSH_ERROR)


def ssh_session_connected(ssh_argv: List[str]) -> bool:
    """Open the session; False when the connection never opened.

    A remote shell exiting non-zero is the user's business, not a failure of the
    lium command — only ssh's own connection failure is.
    """
    try:
        result = subprocess.run(ssh_argv, check=False)

        if result.returncode not in (0, _SSH_CONNECTION_FAILED):
            ui.dim(f"\nSSH session ended with exit code {result.returncode}")
        return result.returncode != _SSH_CONNECTION_FAILED
    except KeyboardInterrupt:
        ui.warning("\nSSH session interrupted")
        return True
    except Exception as e:
        raise CliFailure("ssh_failed", f"Error executing SSH: {e}", EXIT_SSH_ERROR)


def ssh_route_skip_reason(ssh_argv: List[str], host: str) -> Optional[str]:
    """Why dialling ``host`` directly would not test the route ``ssh_argv`` takes, or None.

    Read from ``ssh -G`` on the same arguments, so ``~/.ssh/config``, ``-F`` and ``-o``
    all count. An ssh that can't answer ``-G`` (before OpenSSH 6.8) gives None.
    """
    try:
        resolved = subprocess.run(
            [ssh_argv[0], "-G", *ssh_argv[1:]],
            capture_output=True, text=True, timeout=5, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if resolved.returncode != 0:
        return None
    config: dict = {}
    for line in resolved.stdout.splitlines():
        key, _, value = line.strip().partition(" ")
        config.setdefault(key.lower(), value.strip())
    for key, name in (("proxyjump", "ProxyJump"), ("proxycommand", "ProxyCommand")):
        if config.get(key, "none").lower() != "none":
            return f"ssh config sets {name}"
    hostname = config.get("hostname")
    if hostname and hostname.lower() != host.lower():
        return f"ssh config sends {host} to {hostname}"
    return None


def pod_settled_seconds(pod: PodInfo) -> Optional[float]:
    """Seconds since the later of the pod's ``created_at`` and ``updated_at``; None without a usable one."""
    stamps = [parse_timestamp(s) for s in (pod.created_at, pod.updated_at) if isinstance(s, str) and s]
    stamps = [s if s.tzinfo else s.replace(tzinfo=timezone.utc) for s in stamps if s]
    if not stamps:
        return None
    return (datetime.now(timezone.utc) - max(stamps)).total_seconds()


def wait_for_ssh_banner(pod: PodInfo, ssh_argv: List[str], *, settled_after: Optional[float] = None) -> ActionResult:
    """Wait for the pod's SSH banner under a spinner; ``ok`` False when it never came.

    The caller opens the session either way, and ssh then reports the real error.
    Skipped (``ok`` True, ``data["skipped"]`` says why) when ssh's own config routes
    the session elsewhere, or when ``settled_after`` is given and the pod has not
    changed for longer than that.
    Raises ``ValueError`` for an ``ssh_cmd`` that ssh would not be given.
    """
    _user, host, port = ssh_target(pod.ssh_cmd)
    skipped = ssh_route_skip_reason(ssh_argv, host)
    if skipped is None and settled_after is not None:
        settled = pod_settled_seconds(pod)
        if settled is not None and settled > settled_after:
            skipped = f"pod unchanged for {settled / 60:.0f} min"
    if skipped:
        return ActionResult(ok=True, data={"skipped": skipped})
    action = WaitForSSHAction()
    result = ui.load(f"Waiting for SSH on {host_port(host, port)}", lambda: action.execute({"pod": pod}))
    if not result.ok:
        ui.warning(f"{result.error}; trying ssh anyway")
    return result


def try_once_options(wait: Optional[ActionResult]) -> List[str]:
    """Extra ssh options for the session: a connect timeout after a wait that saw no banner."""
    return list(TRY_ONCE_SSH_OPTIONS) if wait is not None and not wait.ok else []


def ssh_wait_data(wait: Optional[ActionResult]) -> dict:
    """What an SSH failure's ``data`` says about the banner wait before it."""
    if wait is None:
        return {}
    if wait.data.get("skipped"):
        return {"ssh_wait": wait.data}
    return {"ssh_port_answered": wait.ok, "ssh_wait": wait.data}


def ssh_never_answered(pod: PodInfo, wait: ActionResult, data: dict) -> CliFailure:
    """``lium up``'s ``ssh_connection_failed`` for a pod whose SSH port sent no banner, then refused ssh too."""
    huid = pod.huid
    return CliFailure(
        "ssh_connection_failed",
        f"Pod {huid} is RUNNING and billing, but SSH did not answer within "
        f"{wait.data['wait_seconds']:g}s ({host_port(wait.data['host'], wait.data['port'])}: {wait.data['last_problem']}). "
        f"Retry with 'lium ssh {huid}', or remove it with 'lium rm {huid}'",
        EXIT_SSH_ERROR,
        data={**data, **ssh_wait_data(wait)},
        hint=f"The pod was not removed and bills until it is: retry with 'lium ssh {huid}' in a minute, "
             f"or remove it with 'lium rm {huid}' and rent another node",
    )


@click.command("ssh")
@click.argument("target")
@handle_errors
def ssh_command(target: str):
    """Open SSH session to a GPU pod.

    \b
    TARGET: Pod identifier - can be:
      - Pod huid, name or ID (eager-wolf-aa) — the stable form for scripts
      - Row number of your last 'lium ps' in this shell (1, 2) — refused if that pod is gone or the listing is >10 min old

    \b
    Examples:
      lium ssh 1                    # SSH to pod #1 from ps
      lium ssh eager-wolf-aa        # SSH to specific pod
    """

    # Validate
    valid, error = validation.validate(target)
    if not valid:
        if "'ssh' command not found" in error:
            raise CliFailure("ssh_client_missing", error, EXIT_SSH_ERROR)
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, failure = parsing.parse(target, all_pods)
    if failure:
        raise failure

    pod = parsed.get("pod")

    try:
        ssh_ready = wait_for_ssh_banner(pod, lium.ssh_argv(pod), settled_after=POD_SETTLED_SECONDS)
    except ValueError:
        ssh_ready = None  # SshAction refuses the same ssh_cmd below, naming the pod

    # Execute
    ctx = {"lium": lium, "pod": pod, "ssh_options": try_once_options(ssh_ready)}

    action = SshAction()
    result = action.execute(ctx)
    if not result.ok:
        raise CliFailure("ssh_unavailable", result.error, EXIT_SSH_ERROR)

    exit_code = result.data.get("exit_code")
    if exit_code == _SSH_CONNECTION_FAILED:
        raise CliFailure(
            "ssh_failed",
            f"SSH connection to '{pod.huid}' failed",
            EXIT_SSH_ERROR,
            data={"pod_id": pod.id, "pod_name": pod.name, **ssh_wait_data(ssh_ready)},
        )
    if exit_code:
        ui.dim(f"SSH session ended with exit code {exit_code}")
