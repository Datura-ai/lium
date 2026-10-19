"""`lium quote`: what a job on a node costs before renting it."""

import json

import click
from rich.table import Table

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    SIGNUP_NUDGE,
    CliFailure,
    browsing_client,
    handle_errors,
    resolve_executor_indices,
    resolve_output_format,
)


def _node_id(node: str) -> str:
    """`lium ls` row numbers, as `lium up 1` takes them; anything else is a UUID or HUID."""
    if not node.isdigit():
        return node
    ids, error = resolve_executor_indices([node])
    if error or not ids:
        raise CliFailure("node_not_found", error or f"No `lium ls` selection to take row {node} from", hint="lium ls")
    return ids[0]


def _money(value: float | None) -> str:
    return "-" if value is None else f"${value:,.4f}"


def _rate(value: float) -> str:
    """A per-unit rate at full precision: storage rates are fractions of a cent per GB-hour."""
    digits = f"{value:,.8f}".rstrip("0")
    return f"${digits}{'0' * max(0, 2 - len(digits.split('.')[1]))}"


@click.command("quote", epilog="Use --format json for machine-readable output.")
@click.argument("node")
@click.option("--hours", type=float, required=True, help="How long the job runs.")
@click.option("--gpus", "gpu_count", type=int, default=None, help="GPUs to rent (default: the node's free GPUs, as `lium up` rents).")
@click.option("--volume-gb", type=float, default=0.0, show_default=True, help="Volume kept for the job, in GB.")
@click.option("--backup-gb", type=float, default=0.0, show_default=True, help="Backups kept for the job, in GB.")
@click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]), default="table",
    help="Output format. 'json' prints the API's quote.",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def quote_command(node, hours, gpu_count, volume_gb, backup_gb, output_format, json_output):
    """Quote a job on NODE: compute, volume and backup at the rates billing charges.

    NODE is a row number from the last `lium ls`, a node HUID or a UUID. Egress is
    billed at what AWS charges and has no amount. Nothing is reserved, and no API
    key is needed.

    \b
    Examples:
      lium quote 1 --hours 2
      lium quote cosmic-hawk-f2 --hours 24 --gpus 1 --volume-gb 200 --format json
    """
    if hours <= 0:
        raise click.BadParameter("must be above 0", param_hint="--hours")
    json_output = resolve_output_format(output_format, json_output) == "json"
    # the quote route and the listing a HUID resolves through are public: no key is needed to price a job
    lium, anonymous = browsing_client(Lium)
    fetch = lambda: lium.quote(  # noqa: E731
        _node_id(node), hours=hours, gpu_count=gpu_count, volume_gb=volume_gb, backup_gb=backup_gb
    )
    quote = fetch() if json_output else ui.load("Quoting", fetch)

    if json_output:
        click.echo(json.dumps(quote.to_dict(), sort_keys=True))
        return
    table = Table(title=f"{quote.gpu_count} GPU(s) for {quote.hours:g} h at {_money(quote.price_per_hour)}/h")
    for column in ("Item", "Rate", "Quantity", "Amount", "Note"):
        table.add_column(column)
    for line in quote.lines:
        rate = "-" if line.rate_usd is None else f"{_rate(line.rate_usd)}/{line.unit}"
        table.add_row(line.item, rate, f"{line.quantity:g}", _money(line.amount_usd), line.note)
    ui.print(table)
    ui.info(f"Total {_money(quote.total_usd)} (egress not included)")
    if quote.rent_balance_required_usd is None:
        ui.dim(f"This pod's part of the balance a rent needs: {_money(quote.pod_balance_floor_usd)}, plus your open pods")
    else:
        ui.dim(f"Balance this rent needs, with your open pods: more than {_money(quote.rent_balance_required_usd)}")
    if anonymous:
        ui.dim(SIGNUP_NUDGE)
