"""Execute commands on pods using Lium SDK."""
from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Optional, Tuple

import click

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from lium.sdk import Lium, PodInfo
from lium.sdk.client import ENV_NAME
from ..utils import (
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    console,
    handle_errors,
    loading_status,
    parse_targets,
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

    selected_pods = parse_targets(targets, all_pods)
    if selected_pods:
        return selected_pods

    raise CliFailure(
        "pod_not_found", f"No pods match targets: {targets}", EXIT_POD_NOT_FOUND
    )


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
    "--json", "json_output", is_flag=True,
    help="Print machine-readable JSON (stdout, stderr, exit_code) instead of raw output",
)
@handle_errors
def exec_command(
    targets: str,
    command: Optional[str],
    script: Optional[str],
    env: Tuple[str],
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

    \b
    The process exits with the remote command's exit code, so
    'lium exec <pod> "cmd" && next-step' behaves the way a caller expects.
    """
    command_to_run = resolve_command_to_run(command, script)
    env_dict = parse_environment_variables(env)

    lium = Lium()
    selected_pods = resolve_pods_or_fail(lium, targets, show_progress=not json_output)

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

    if len(selected_pods) == 1:
        results = [lium.exec(selected_pods[0], command=command_to_run, env=env_dict)]
    else:
        results = lium.exec_all(selected_pods, command=command_to_run, env=env_dict)

    executions = [
        PodExecution.from_sdk_result(pod, result)
        for pod, result in zip(selected_pods, results)
    ]
    report_executions(executions, json_output)

    # Any pod failing fails the whole invocation, like a shell pipeline would.
    raise SystemExit(max(execution.exit_code for execution in executions))
