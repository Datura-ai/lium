"""`lium record`: what one run ran on and what it cost, after the pod is gone too (H100 item 27)."""

import json
import uuid

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import CliFailure, handle_errors, parse_targets, resolve_output_format


def _pod_id(lium: Lium, target: str) -> str:
    """A pod UUID as given (a removed pod has no other name); a live pod's name, HUID or `lium ps` row otherwise."""
    try:
        return str(uuid.UUID(target))
    except ValueError:
        pass
    matches = parse_targets(target, lium.ps())
    if len(matches) != 1:
        raise CliFailure(
            "pod_not_found",
            f"'{target}' names {len(matches)} running pods; give one pod, or a removed pod's UUID",
            hint="lium ps",
        )
    return matches[0].id


def _line(label: str, value) -> None:
    if value not in (None, ""):
        ui.print(f"{label:<13}{value}")


@click.command("record", epilog="Use --format json for machine-readable output.")
@click.argument("pod")
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]), default="table",
    help="Output format. 'json' prints the server's run record.",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def record_command(pod, output_format, json_output):
    """Show what POD ran on and what it cost, to repeat or cite the run.

    POD is a pod UUID (works after the pod is removed), or a running pod's name,
    HUID or `lium ps` row. Shows the GPU, the image (with its digest when the
    template pins one), the node's driver, CUDA and interconnect when the
    container came up, start and end, why it ended, and what the ledger charged.

    \b
    Examples:
      lium record 1
      lium record 3f2b9c1e-8a4d-4b6e-9f10-2c3d4e5f6a7b --format json | jq .launch_snapshot
    """
    json_output = resolve_output_format(output_format, json_output) == "json"
    lium = Lium()
    fetch = lambda: lium.pod_record(_pod_id(lium, pod))  # noqa: E731
    record = fetch() if json_output else ui.load("Loading the run record", fetch)

    if json_output:
        click.echo(json.dumps(record, sort_keys=True))
        return
    env = record.get("launch_snapshot") or {}
    count = f"{record['gpu_count']}× " if record.get("gpu_count") is not None else ""
    gpus = f"{count}{record['gpu_name']}" if record.get("gpu_name") else None
    _line("Pod", f"{record.get('pod_name') or ''} {record['pod_id']}{' (removed)' if record.get('removed') else ''}")
    _line("GPU", gpus)
    _line("Image", record.get("image"))
    _line("Driver", " / CUDA ".join(v for v in (env.get("gpu_driver"), env.get("cuda_driver")) if v))
    _line("Interconnect", env.get("gpu_interconnect"))
    _line("Node", record.get("executor_id"))
    _line("Started", record.get("created_at"))
    reason = f" ({record['close_reason']})" if record.get("close_reason") else ""
    _line("Ended", record.get("removed_at") and f"{record['removed_at']}{reason}")
    _line("Charged", f"${record.get('total', 0):,.4f} for {record.get('billed_seconds', 0) / 3600:.2f} h billed")
    if not env:
        ui.dim("No launch snapshot: the run started before it was recorded, or its container never came up.")
