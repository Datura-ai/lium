"""`lium request`: ask providers for a GPU type that is sold out."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors


@click.command("request")
@click.argument("gpu")
@click.option("--count", "-n", type=int, default=None, help="GPUs wanted on one node.")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def request_command(gpu: str, count, json_output: bool):
    """Ask providers for a GPU type that is sold out.

    The providers who list that GPU type are told, and the request shows on
    https://lium.io/machine-requests.

    \b
    Examples:
      lium request B300 -n 8
      lium request H200 --json
    """
    answer = Lium().request_machine(gpu, count)
    if json_output:
        click.echo(json.dumps(answer, sort_keys=True))
        return
    ui.success(answer.get("confirmation") or f"Request sent to {answer.get('provider_notified_count') or 0} providers.")
