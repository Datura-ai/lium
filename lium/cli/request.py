"""`lium request`: ask providers for a GPU type that is sold out."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors

UTC_TIME = click.DateTime(formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"])


@click.command("request")
@click.argument("gpu")
@click.option("--count", "-n", type=int, default=None, help="GPUs wanted on one node.")
@click.option("--auto-rent", is_flag=True, help="Rent the cheapest matching node as soon as one lists.")
@click.option("--max-price", type=float, default=None, help="Auto-rent: highest USD per GPU-hour.")
@click.option("--hours", type=click.IntRange(1, 720), default=None, help="Auto-rent: how long the pod runs.")
@click.option("--template", "-t", "template", default=None, help="Auto-rent: template ID, HUID or name.")
@click.option("--start-after", type=UTC_TIME, default=None, help="Earliest start, UTC (YYYY-MM-DD[THH:MM]).")
@click.option("--start-before", type=UTC_TIME, default=None, help="Latest start, UTC (YYYY-MM-DD[THH:MM]).")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def request_command(gpu: str, count, auto_rent, max_price, hours, template, start_after, start_before, json_output):
    """Ask providers for a GPU type that is sold out.

    The providers who list that GPU type may be notified, and the request shows
    on https://lium.io/machine-requests.

    With --auto-rent, Lium rents the cheapest node that matches within the
    window at or below --max-price, for --hours, with your configured SSH keys.
    It needs --count, --max-price, --hours, --template and a paid top-up.

    \b
    Examples:
      lium request B300 -n 8
      lium request H200 --json
      lium request B200 -n 8 --auto-rent --max-price 4.5 --hours 24 -t pytorch --start-before 2026-11-10
    """
    if auto_rent:
        missing = [flag for flag, value in (("--count", count), ("--max-price", max_price), ("--hours", hours), ("--template", template)) if not value]
        if missing:
            raise click.UsageError(f"--auto-rent needs {', '.join(missing)}")
    answer = Lium().request_machine(
        gpu,
        count,
        auto_rent=auto_rent,
        max_price_per_gpu_hour=max_price,
        hours=hours,
        template_id=template,
        start_after=start_after,
        start_before=start_before,
    )
    if json_output:
        click.echo(json.dumps(answer, sort_keys=True))
        return
    ui.success(answer.get("confirmation") or f"Request sent to {answer.get('provider_notified_count') or 0} providers.")
