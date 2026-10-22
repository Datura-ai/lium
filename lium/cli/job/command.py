"""`lium job run`: one bounded GPU job from the shell, the CLI face of `Lium.run_job` (H100 item 31)."""

import json
import re

import click

from lium.sdk import Lium
from lium.sdk.bounded_job import FAILED, SUCCEEDED, TIMED_OUT, BUDGET_EXHAUSTED
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smh]?)$")
_SECONDS = {"": 1, "s": 1, "m": 60, "h": 3600}


def _duration(value: str) -> float:
    match = _DURATION.match(value.strip().lower())
    if not match or float(match.group(1)) <= 0:
        raise click.BadParameter(f"{value!r}: use seconds or a number with s, m or h (90, 30m, 2h)")
    return float(match.group(1)) * _SECONDS[match.group(2)]


def _pairs(values, sep: str, what: str) -> dict:
    pairs = {}
    for value in values:
        left, found, right = value.partition(sep)
        if not found or not left or not right:
            raise click.BadParameter(f"{value!r}: expected {what}")
        pairs[left] = right
    return pairs


def exit_code_for(result) -> int:
    """0 on success, the command's own code when it failed, 124 when a limit stopped it (as `timeout`), else 1."""
    if result.status == SUCCEEDED:
        return 0
    if result.status == FAILED and result.exit_code:
        return result.exit_code
    if result.status in (TIMED_OUT, BUDGET_EXHAUSTED):
        return 124
    return 1


@click.group("job")
def job_command():
    """Run bounded GPU jobs: rent, run, copy back, remove the pod."""


@job_command.command("run", epilog="Use --format json for machine-readable output.")
@click.argument("command")
@click.option("--gpu", "gpu_type", required=True, help="GPU type, as in `lium ls` (e.g. H100).")
@click.option("--count", "gpu_count", type=int, default=1, show_default=True, help="GPUs on the pod.")
@click.option("--budget", "max_cost_usd", type=float, required=True, help="Approximate compute budget in USD, boot and copying included.")
@click.option("--deadline", required=True, help="Wall-clock ceiling: seconds, or 30m / 2h. Boot and copying included.")
@click.option("--input", "inputs", multiple=True, metavar="LOCAL:REMOTE", help="Upload before the command. Repeatable.")
@click.option("--output", "outputs", multiple=True, metavar="REMOTE:LOCAL", help="Download after it ends, failed or not. Repeatable.")
@click.option("--env", "env", multiple=True, metavar="KEY=VALUE", help="Environment for the command. Repeatable.")
@click.option("--max-price-per-gpu-hour", type=float, default=None, help="Skip nodes priced above this.")
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]), default="table",
    help="Output format. 'json' prints the job result as one JSON object.",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def run_command(command, gpu_type, gpu_count, max_cost_usd, deadline, inputs, outputs, env,
                max_price_per_gpu_hour, output_format, json_output):
    """Run COMMAND on a rented GPU within a budget and a deadline, then remove the pod.

    The pod's removal is scheduled server-side at the earlier limit right after the rent; once
    that is acknowledged it survives this process exiting (a crash during the rent can still leave
    a pod without it). The budget is approximate: removal is processed periodically, so charges can
    exceed it. Outputs are copied back after a failure or a timeout too.
    Exits 0 on success, the command's exit code when it failed, 124 when the budget or deadline
    stopped it, and 1 otherwise.

    \b
    Examples:
      lium job run "python train.py" --gpu H100 --budget 5 --deadline 1h \\
          --input train.py:/root/train.py --output /root/model.pt:model.pt
      lium job run "nvidia-smi" --gpu H100 --budget 1 --deadline 10m --format json | jq .status
    """
    if max_cost_usd <= 0:
        raise click.BadParameter("must be above 0", param_hint="--budget")
    json_output = resolve_output_format(output_format, json_output) == "json"
    rent_kwargs = {}
    if max_price_per_gpu_hour is not None:
        rent_kwargs["max_price_per_gpu_hour"] = max_price_per_gpu_hour
    job = dict(
        command=command, gpu_type=gpu_type, gpu_count=gpu_count, max_cost_usd=max_cost_usd,
        deadline_s=_duration(deadline),
        inputs=_pairs(inputs, ":", "LOCAL:REMOTE"), outputs=_pairs(outputs, ":", "REMOTE:LOCAL"),
        env=_pairs(env, "=", "KEY=VALUE") or None, **rent_kwargs,
    )
    lium = Lium()
    result = lium.run_job(**job) if json_output else ui.load("Running the job", lambda: lium.run_job(**job))

    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    else:
        if result.stdout:
            click.echo(result.stdout, nl=not result.stdout.endswith("\n"))
        if result.stderr:
            click.echo(result.stderr, err=True, nl=not result.stderr.endswith("\n"))
        cost = result.cost_usd if result.cost_usd is not None else result.estimated_cost_usd
        cost_text = "unknown" if cost is None else f"${cost:,.4f}" + ("" if result.cost_usd is not None else " (estimate)")
        summary = f"{result.status}: exit {result.exit_code}, pod {result.cleanup}, cost {cost_text}, {result.seconds:.0f}s"
        (ui.info if result.ok else ui.warning)(summary)
        if result.error:
            ui.dim(result.error)
        for remote, local in result.outputs.items():
            ui.dim(f"copied {remote} -> {local}")
        for remote, reason in result.missing_outputs.items():
            ui.warning(f"not copied {remote}: {reason}")
    raise SystemExit(exit_code_for(result))
