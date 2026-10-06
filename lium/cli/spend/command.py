"""`lium spend`: what the active pods cost and how long the balance lasts."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format
from lium.cli.workspaces.context import show_workspace
from . import report as report_module


def _gather(lium: Lium):
    pods = lium.ps()
    try:
        balance = lium.balance()
    except Exception as exc:  # noqa: BLE001 - the per-pod figures are still worth showing
        ui.notice_debug(f"balance unavailable: {exc}")
        balance = None
    return report_module.build_report(pods, balance)


@click.command("spend", epilog="Use --format json for machine-readable output.")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def spend_command(output_format: str, json_output: bool):
    """Show the hourly burn, the estimated spend per active pod, and the runway left.

    Spent is price × wall time since each pod was created (a PENDING pod is $0:
    it is not billing yet); the API does not report billed amounts, so treat it
    as an estimate. Burn counts the pods the platform bills (RUNNING and reboot
    states).

    \b
    Examples:
      lium spend
      lium spend --format json | jq '.burn_per_hour, .runway_hours'
    """
    json_output = resolve_output_format(output_format, json_output) == "json"
    lium = Lium()
    report = _gather(lium) if json_output else ui.load("Loading pods and balance", lambda: _gather(lium))

    if json_output:
        click.echo(json.dumps(report.to_dict(), sort_keys=True))
        return

    if not report.pods:
        ui.info("No active pods; burn is $0.00/h")
        if report.balance_usd is not None:
            ui.dim(f"Balance ${report.balance_usd:,.2f}")
        show_workspace(lium)   # whose balance and runway this is (lium#183), as ps/ls say under their tables
        return

    ui.print(report_module.build_table(report))
    for line in report_module.summary_lines(report):
        ui.dim(line)
    show_workspace(lium)
