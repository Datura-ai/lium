"""Execute commands on pods using Lium SDK."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lium.sdk import Lium, PodInfo, ssh_mux
from lium.sdk.client import ENV_NAME
from lium.sdk.detach import (
    DEFAULT_DETACH_LOG_DIR,
    build_detached_command,
    default_detach_log_path,
    detach_token,
)
from ..utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    console,
    _exact_match,
    handle_errors,
    loading_status,
    parse_targets,
    remember_pods,
    remembered_pods,
)

# The remote command failed but said nothing about how. Its own status space, not
# the lium process exit table.
UNKNOWN_REMOTE_FAILURE = 1

# pip on an Ubuntu 24.04 image refuses to touch the system Python (PEP 668) and
# prints Debian's apt/pipx advice, none of which applies to a GPU pod. Only the
# marker is visible here, not the image: a `--image`/`--dockerfile` pod may ship
# no torch at all, so the hint promises nothing about what the system Python holds.
PEP668_MARKER = "externally-managed-environment"
PEP668_HINT = (
    "Hint: pip on this image is PEP 668-managed; --system-site-packages keeps any torch the image ships. Either:",
    "  pip install --break-system-packages <pkg>",
    "  python3 -m venv --system-site-packages /workspace/venv && /workspace/venv/bin/pip install <pkg>",
)


@dataclass(frozen=True)
class PodExecution:
    """What one pod printed and how its command ended."""

    pod: str
    stdout: str
    stderr: str
    exit_code: int
    error: str | None

    @classmethod
    def from_sdk_result(cls, pod: PodInfo, result: Mapping[str, object]) -> "PodExecution":
        # The SDK omits exit_code only when it could not run the command at all.
        exit_code = result.get("exit_code")
        return cls(
            pod=pod.huid,
            stdout=str(result.get("stdout") or ""),
            stderr=str(result.get("stderr") or ""),
            exit_code=exit_code if isinstance(exit_code, int) else UNKNOWN_REMOTE_FAILURE,
            error=str(result["error"]) if result.get("error") else None,
        )

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0


def print_execution_for_a_human(execution: PodExecution, show_pod_header: bool) -> None:
    """Print output the same way whether the command succeeded or not.

    The log a caller needs to diagnose a failure is exactly the log a failing
    command produced, so dropping it on failure blinds the caller at the worst
    moment.
    """
    if show_pod_header:
        console.info(f"\n── {execution.pod} ──")

    if execution.stdout:
        print(execution.stdout, end="")
    if execution.stderr:
        # Real stderr, unstyled: a caller reading the stream must not have to
        # strip Rich markup out of the command's own output.
        click.echo(execution.stderr, err=True, nl=False)

    if not execution.succeeded:
        if execution.error:
            console.error(f"Error: {execution.error}")
        else:
            console.error(f"Command failed (exit code: {execution.exit_code})")
        if PEP668_MARKER in execution.stderr or PEP668_MARKER in execution.stdout:
            for line in PEP668_HINT:
                console.dim(line, soft_wrap=True)  # one copy-pasteable command per line


DETACH_SCRIPT_DIR = "/tmp"


def build_script_upload_command(script_text: str, remote_path: str) -> str:
    """Write ``script_text`` to ``remote_path`` on the pod, byte for byte.

    Base64 keeps quoting out of it: the script may contain anything.
    """
    encoded = base64.b64encode(script_text.encode("utf-8")).decode("ascii")
    quoted_path = shlex.quote(remote_path)
    return (
        f"printf %s {shlex.quote(encoded)} | base64 -d > {quoted_path} "
        f"&& chmod +x {quoted_path}"
    )


def build_detached_script_command(script_text: str, log_path: str, token: str, prelude: str = "") -> str:
    """Copy the script to the pod, then start it detached and print its PID.

    ``prelude`` runs inside the detached login shell before the script (the
    ``-e`` exports, see :meth:`Lium.login_shell_env`).
    """
    remote_script = f"{DETACH_SCRIPT_DIR}/lium-exec-{token}.sh"
    return (
        f"{build_script_upload_command(script_text, remote_script)} && "
        f"{build_detached_command(prelude + remote_script, log_path)}"
    )


def parse_detached_pid(stdout: str) -> Optional[int]:
    """The PID ``echo $!`` printed, or None when the launcher did not get that far."""
    for token in (stdout or "").split():
        if token.isdigit():
            return int(token)
    return None


@dataclass(frozen=True)
class DetachedExecution:
    """What one pod reported after being asked to start a command in the background."""

    pod: str
    pid: int | None
    log: str
    error: str | None

    @classmethod
    def from_sdk_result(cls, pod: PodInfo, result: Mapping[str, object], log_path: str) -> "DetachedExecution":
        pid = parse_detached_pid(str(result.get("stdout") or ""))
        error = str(result["error"]) if result.get("error") else None
        if pid is None and error is None:
            stderr = str(result.get("stderr") or "").strip()
            error = stderr or f"Launcher exited with code {result.get('exit_code')} without printing a PID"
        return cls(pod=pod.huid, pid=pid, log=log_path, error=error)

    @property
    def succeeded(self) -> bool:
        return self.pid is not None


def report_detached_executions(executions: list[DetachedExecution], json_output: bool) -> None:
    if json_output:
        payload = {
            "ok": all(execution.succeeded for execution in executions),
            "results": [asdict(execution) for execution in executions],
        }
        click.echo(json.dumps(payload, sort_keys=True))
        return

    for execution in executions:
        if execution.succeeded:
            console.success(
                f"Started on {console.get_styled(execution.pod, 'pod_id')}: "
                f"PID {execution.pid}, log {execution.log}"
            )
            # `lium exec` returns the command's output once it exits, so the
            # hint is a bounded read; a `tail -f` there would never come back.
            console.dim(f"  follow with: lium exec {execution.pod} \"tail -n 200 {execution.log}\"")
        else:
            console.error(f"Failed to start on {execution.pod}: {execution.error}")


def resolve_command_to_run(command: Optional[str], script: Optional[str]) -> str:
    """The command text to run remotely, from either COMMAND or --script."""
    if not command and not script:
        raise CliFailure(
            "missing_command",
            "Either COMMAND or --script must be provided",
            EXIT_CONFIGURATION_ERROR,
        )

    if command and script:
        raise CliFailure(
            "conflicting_command",
            "Cannot use both COMMAND and --script",
            EXIT_CONFIGURATION_ERROR,
        )

    if command:
        return command

    try:
        return Path(script).read_text()
    except OSError as e:
        raise CliFailure("unreadable_script", f"Error reading script: {e}", EXIT_CONFIGURATION_ERROR)


def parse_environment_variables(env: Tuple[str, ...]) -> dict[str, str]:
    """KEY=VALUE pairs from -e, rejecting anything that is not a pair."""
    env_dict: dict[str, str] = {}
    for env_var in env:
        if "=" not in env_var:
            raise CliFailure(
                "invalid_env",
                f"Invalid env format '{env_var}' (use KEY=VALUE)",
                EXIT_CONFIGURATION_ERROR,
            )
        key, value = env_var.split("=", 1)
        if not ENV_NAME.fullmatch(key):
            raise CliFailure(
                "invalid_env",
                f"Invalid env name {key!r} (letters, digits and underscores; not starting with a digit)",
                EXIT_CONFIGURATION_ERROR,
            )
        env_dict[key] = value
    return env_dict


def resolve_pods_or_fail(lium: Lium, targets: str, show_progress: bool) -> list[PodInfo]:
    """Pods matching TARGETS. A target that matches nothing is a hard failure.

    The progress spinner writes to stdout, which belongs to the JSON consumer.
    """
    with loading_status("Loading pods", "") if show_progress else contextlib.nullcontext():
        all_pods = lium.ps()
    if uses_control_master(lium):
        remember_pods(lium, all_pods)

    selected_pods = parse_targets(targets, all_pods)
    if selected_pods:
        return selected_pods

    raise CliFailure(
        "pod_not_found", f"No pods match targets: {targets}", EXIT_POD_NOT_FOUND
    )


def uses_control_master(lium: Lium) -> bool:
    """Whether commands go over a persistent OpenSSH connection (see :mod:`lium.sdk.ssh_mux`)."""
    return ssh_mux.available() and bool(lium.config.ssh_key_path)


def pods_with_live_masters(lium: Lium, targets: str) -> Optional[list[PodInfo]]:
    """The pods TARGETS names, from the pod cache, when each already has a live control master.

    None sends the caller to the pod list: a target missing from the cache or with no live
    master, ``all`` (the list is the answer), and row numbers, which are checked against
    the live list (see :func:`resolve_targets`).
    """
    if targets.strip().lower() == "all" or not uses_control_master(lium):
        return None
    names = [name.strip() for name in targets.split(",") if name.strip()]
    if not names or any(name.isdigit() for name in names):
        return None
    cached = remembered_pods(lium)
    if not cached:
        return None
    pods = []
    for name in names:
        pod = _exact_match(name, cached)
        if pod is None or not ssh_mux.has_live_master(lium, pod):
            return None
        pods.append(pod)
    return pods


def report_executions(executions: list[PodExecution], json_output: bool) -> None:
    """Render every pod's result, then leave the exit code to the caller."""
    if json_output:
        payload = {
            "ok": all(execution.succeeded for execution in executions),
            "results": [asdict(execution) for execution in executions],
        }
        click.echo(json.dumps(payload, sort_keys=True))
        return

    show_pod_header = len(executions) > 1
    for execution in executions:
        print_execution_for_a_human(execution, show_pod_header=show_pod_header)

    if show_pod_header:
        succeeded = sum(1 for execution in executions if execution.succeeded)
        console.dim(f"\nCompleted: {succeeded}/{len(executions)} successful")


@click.command("exec")
@click.argument("targets")
@click.argument("command", required=False)
@click.option("--script", "-s", help="Execute a script file")
@click.option("--env", "-e", multiple=True, help="Set environment variables (KEY=VALUE)")
@click.option(
    "--detach", "-d", is_flag=True,
    help="Start the command in the background on the pod (nohup + setsid, stdin closed), print its PID and log path, and return immediately",
)
@click.option(
    "--log", "log_path",
    help=f"Log file for --detach on the pod (default: {DEFAULT_DETACH_LOG_DIR}/exec-<timestamp>-<id>.log)",
)
@click.option(
    "--json", "json_output", is_flag=True,
    help="Print machine-readable JSON (stdout, stderr, exit_code) instead of raw output",
)
@handle_errors
def exec_command(
    targets: str,
    command: Optional[str],
    script: Optional[str],
    env: Tuple[str],
    detach: bool,
    log_path: Optional[str],
    json_output: bool,
):
    """Execute commands on GPU pods.
    
    \b
    TARGETS: Pod identifiers - can be:
      - Pod huid, name or ID (eager-wolf-aa) — the stable form for scripts
      - Row number of your last 'lium ps' in this shell (1, 2) — refused if that pod is gone or the listing is >10 min old
      - Comma-separated (1,2,eager-wolf-aa)
      - All pods (all)
    
    COMMAND: Command to execute
    
    \b
    Examples:
      lium exec eager-wolf-aa "nvidia-smi"     # Run on specific pod
      lium exec 1 "python --version"           # Run on pod #1 from ps
      lium exec 1,2,3 "uptime"                 # Run on multiple pods
      lium exec all "df -h"                    # Run on all pods
      lium exec 1 --script setup.sh            # Run script on pod
      lium exec 1 -e API_KEY=xyz "python app.py"  # With env vars
      lium exec 1 --json "python train.py"     # Machine-readable result
      lium exec 1 -d "python train.py"         # Start in background, print PID and log path
      lium exec 1 -d --log /workspace/train.log --script train.sh

    \b
    The process exits with the remote command's exit code, so
    'lium exec <pod> "cmd" && next-step' behaves the way a caller expects.
    With --detach it exits 0 once the command has been started.

    \b
    The SSH connection stays open for 10 minutes after the last command
    (LIUM_SSH_PERSIST=<seconds>; 0 connects per command), so the next
    'lium exec' to the same pod starts without a new connection or pod lookup.
    """
    command_to_run = resolve_command_to_run(command, script)
    env_dict = parse_environment_variables(env)
    if log_path and not detach:
        raise CliFailure(
            "invalid_arguments", "--log only applies together with --detach", EXIT_CONFIGURATION_ERROR
        )

    lium = Lium()
    selected_pods = pods_with_live_masters(lium, targets) or resolve_pods_or_fail(
        lium, targets, show_progress=not json_output
    )

    if not json_output:
        if len(selected_pods) == 1:
            console.info(f"Executing on {console.get_styled(selected_pods[0].huid, 'pod_id')}")
        else:
            console.info(f"Executing on {len(selected_pods)} pods")

        if env_dict:
            # Names only, values masked. -e is a common way to pass tokens, and
            # echoing the values here lands secrets in terminal logs and agent
            # transcripts. The values still reach the pod unchanged.
            masked = ", ".join(f"{name}=****" for name in env_dict)
            console.dim(f"Environment: {masked}")

    if detach:
        token = detach_token()
        log_path = log_path or default_detach_log_path(token)
        # The detached process runs under a login shell whose profile would win
        # over an inherited value: the -e exports travel as one variable and are
        # applied inside that shell instead (names only ever reach the pod's argv).
        prelude, env_dict = lium.login_shell_env(env_dict)
        # A script is copied to the pod first so the detached process runs it
        # from a file rather than from a command line that ends with this session.
        if script:
            command_to_run = build_detached_script_command(command_to_run, log_path, token, prelude=prelude)
        else:
            command_to_run = build_detached_command(prelude + command_to_run, log_path)

    if uses_control_master(lium):
        if len(selected_pods) == 1:
            results = [ssh_mux.exec_over_master(lium, selected_pods[0], command=command_to_run, env=env_dict)]
        else:
            results = ssh_mux.exec_all_over_masters(lium, selected_pods, command=command_to_run, env=env_dict)
    elif len(selected_pods) == 1:
        results = [lium.exec(selected_pods[0], command=command_to_run, env=env_dict)]
    else:
        results = lium.exec_all(selected_pods, command=command_to_run, env=env_dict)

    if detach:
        detached = [
            DetachedExecution.from_sdk_result(pod, result, log_path)
            for pod, result in zip(selected_pods, results)
        ]
        report_detached_executions(detached, json_output)
        if not all(execution.succeeded for execution in detached):
            raise SystemExit(EXIT_GENERAL_ERROR)
        return

    executions = [
        PodExecution.from_sdk_result(pod, result)
        for pod, result in zip(selected_pods, results)
    ]
    report_executions(executions, json_output)

    # Any pod failing fails the whole invocation, like a shell pipeline would.
    raise SystemExit(max(execution.exit_code for execution in executions))
