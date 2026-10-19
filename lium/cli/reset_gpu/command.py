"""Reset-gpu command implementation."""

import json
from typing import Tuple

import click

from lium.sdk import Lium, LiumGpuOperationError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    narrate_on_stderr_under_json,
    parse_targets,
    sdk_error_failure,
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
        question = f"Reset {which} of {pod.huid}? This stops all processes in it; files are kept."
        if not ui.confirm(question, hint="pass --yes to reset without a prompt", stderr=json_output):
            ui.warning("Nothing reset")
            return

    try:
        result = ui.load(f"Resetting {which} of {pod.huid}", lambda: lium.reset_gpu(pod, list(gpus) or None))
    except LiumGpuOperationError as e:
        # the reset ran and a GPU failed: show which, then fail with the API's code and every GPU's outcome
        if not json_output:
            _table(e.gpus)
            if e.container_restarted is False:
                ui.warning(f"The container of {pod.huid} is still stopped.")
        raise sdk_error_failure(e, {"container_restarted": e.container_restarted, "gpus": [g.to_dict() for g in e.gpus]})

    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    else:
        _table(result.gpus)
        ui.success(f"GPU reset done for {pod.huid}")


def _table(gpus) -> None:
    ui.table(["GPU", "UUID", "Result", "Detail"], [[str(g.index), g.uuid, g.result, g.detail or ""] for g in gpus])
