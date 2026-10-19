"""Gpu-clocks command implementation."""

import json
from typing import Optional, Tuple

import click

from lium.sdk import Lium, LiumGpuOperationError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    handle_errors,
    narrate_on_stderr_under_json,
    one_pod,
    sdk_error_failure,
)


def _clock_range(ctx, param, value: Optional[str]):
    if value is None:
        return None
    try:
        lo, hi = (int(v) for v in value.split(","))
    except ValueError:
        raise click.BadParameter("expected MIN,MAX in MHz, e.g. 1400,1800")
    if lo <= 0 or hi < lo:
        raise click.BadParameter("MIN must be positive and not above MAX")
    return lo, hi


@click.command("gpu-clocks")
@click.argument("target")
@click.option("--lock", "lock", metavar="MIN,MAX", callback=_clock_range,
              help="Lock the GPU core clock to this range in MHz, e.g. 1400,1800.")
@click.option("--reset", is_flag=True, help="Return the GPU clocks to the driver default.")
@click.option(
    "--gpu", "gpus", multiple=True, type=click.IntRange(min=0), metavar="N",
    help="Pod GPU index as nvidia-smi shows it inside the pod; repeat for more. Default: all of the pod's GPUs.",
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
@narrate_on_stderr_under_json
def gpu_clocks_command(target: str, lock: Optional[Tuple[int, int]], reset: bool, gpus: Tuple[int, ...], json_output: bool):
    """Lock a pod's GPU clocks to a range, or reset them to the default.

    Nothing in the pod is stopped.

    \b
    Examples:
      lium gpu-clocks my-pod --lock 1400,1800
      lium gpu-clocks my-pod --lock 1400,1800 --gpu 0 --gpu 1
      lium gpu-clocks my-pod --reset --json
    """
    if bool(lock) == reset:
        raise CliFailure("invalid_arguments", "Pass exactly one of --lock MIN,MAX or --reset", EXIT_CONFIGURATION_ERROR)
    lium = Lium()
    pod = one_pod(target, ui.load("Loading pods", lambda: lium.ps()), "gpu-clocks")

    which = f"GPU {', '.join(map(str, gpus))}" if gpus else "all GPUs"
    indices = list(gpus) or None
    try:
        if reset:
            result = ui.load(f"Resetting clocks of {which} of {pod.huid}", lambda: lium.reset_gpu_clocks(pod, indices))
        else:
            result = ui.load(f"Locking clocks of {which} of {pod.huid} to {lock[0]}-{lock[1]} MHz",
                             lambda: lium.lock_gpu_clocks(pod, lock[0], lock[1], indices))
    except LiumGpuOperationError as e:
        # part of the change ran: show which GPUs are locked and which failed, then fail with the API's code
        if not json_output:
            _table(e.gpus)
        raise sdk_error_failure(e, {"gpus": [g.to_dict() for g in e.gpus]})

    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    else:
        _table(result.gpus)
        ui.success(f"GPU clocks {'reset' if reset else 'locked'} on {pod.huid}")


def _mhz(value) -> str:
    return "" if value is None else str(value)


def _table(gpus) -> None:
    ui.table(
        ["GPU", "UUID", "Result", "Min MHz", "Max MHz", "Detail"],
        [[str(g.index), g.uuid, g.result, _mhz(g.applied_min_mhz), _mhz(g.applied_max_mhz), g.detail or ""]
         for g in gpus],
    )
