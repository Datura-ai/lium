"""Gpu-clocks command implementation."""

import json
from typing import Optional, Tuple

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
    all_pods = ui.load("Loading pods", lambda: lium.ps())
    selected = parse_targets(target, all_pods) if all_pods else []
    if not selected:
        raise CliFailure("pod_not_found", f"No pods match targets: {target}", EXIT_POD_NOT_FOUND)
    if len(selected) > 1:
        raise CliFailure("invalid_arguments", "gpu-clocks takes one pod", EXIT_CONFIGURATION_ERROR)
    pod = selected[0]

    which = f"GPU {', '.join(map(str, gpus))}" if gpus else "all GPUs"
    indices = list(gpus) or None
    if reset:
        result = ui.load(f"Resetting clocks of {which} of {pod.huid}", lambda: lium.reset_gpu_clocks(pod, indices))
    else:
        result = ui.load(f"Locking clocks of {which} of {pod.huid} to {lock[0]}-{lock[1]} MHz",
                         lambda: lium.lock_gpu_clocks(pod, lock[0], lock[1], indices))

    if not json_output:
        ui.table(
            ["GPU", "UUID", "Result", "Min MHz", "Max MHz", "Detail"],
            [[str(g.get("index", "")), g.get("uuid") or "", g.get("result") or "",
              "" if g.get("applied_min_mhz") is None else str(g["applied_min_mhz"]),
              "" if g.get("applied_max_mhz") is None else str(g["applied_max_mhz"]),
              g.get("detail") or ""]
             for g in result.get("gpus") or []],
        )
    if not result.get("ok"):
        # a 200 with ok=false: at least one GPU failed — a script must not read it as done
        raise CliFailure("gpu_clocks_failed", f"GPU clock change on {pod.huid} did not succeed", EXIT_GENERAL_ERROR, data=result)
    if json_output:
        click.echo(json.dumps(result, sort_keys=True))
    else:
        ui.success(f"GPU clocks {'reset' if reset else 'locked'} on {pod.huid}")
