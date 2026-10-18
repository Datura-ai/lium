"""Reset-gpu command implementation."""

import json
from typing import Tuple

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    narrate_on_stderr_under_json,
    parse_targets,
)


@click.command("reset-gpu")
@click.argument("target")
@click.option(
    "--gpu", "gpus", multiple=True, type=click.IntRange(min=0), metavar="N",
    help="Pod GPU index as nvidia-smi shows it inside the pod; repeat for more. Default: all of the pod's GPUs.",
)
@click.option(
    "--yes", "-y", is_flag=True,
    help="Skip the confirmation prompt; required when stdin is not a terminal (scripts, pipes).",
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
@narrate_on_stderr_under_json
def reset_gpu_command(target: str, gpus: Tuple[int, ...], yes: bool, json_output: bool):
    """Reset a pod's hung GPUs without recreating the pod.

    Stops every process in the pod (even when one GPU is named), resets the GPUs and
    starts the pod again. Files are kept.

    \b
    Examples:
      lium reset-gpu my-pod
      lium reset-gpu my-pod --gpu 0 --gpu 2
      lium reset-gpu my-pod --yes --json
    """
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())
    selected = parse_targets(target, all_pods) if all_pods else []
    if not selected:
        raise CliFailure("pod_not_found", f"No pods match targets: {target}", EXIT_POD_NOT_FOUND)
    if len(selected) > 1:
        raise CliFailure("invalid_arguments", "reset-gpu takes one pod", EXIT_CONFIGURATION_ERROR)
    pod = selected[0]

    which = f"GPU {', '.join(map(str, gpus))}" if gpus else "all GPUs"
    if not yes:
        ui.warning(f"This stops all processes in {pod.huid}; files are kept.")
        if not ui.confirm(f"Reset {which} of {pod.huid}?", hint="pass --yes to reset without a prompt", stderr=json_output):
            ui.warning("Nothing reset")
            return

    result = ui.load(f"Resetting {which} of {pod.huid}", lambda: lium.reset_gpu(pod, list(gpus) or None))

    if not json_output:
        ui.table(
            ["GPU", "UUID", "Result", "Detail"],
            [[str(g.get("index", "")), g.get("uuid") or "", g.get("result") or "", g.get("detail") or ""]
             for g in result.get("gpus") or []],
        )
    if not result.get("ok"):
        # a 200 with ok=false: the reset ran and at least one GPU failed — a script must not read it as done
        raise CliFailure("gpu_reset_failed", f"GPU reset of {pod.huid} did not succeed", EXIT_GENERAL_ERROR, data=result)
    if json_output:
        click.echo(json.dumps(result, sort_keys=True))
    else:
        ui.success(f"GPU reset done for {pod.huid}")
