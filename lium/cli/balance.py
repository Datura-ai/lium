"""Balance command."""

import json

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format


@click.command("balance", epilog="Use --format json (or --json) for machine-readable output.")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, help="Alias for --format json")
@handle_errors
def balance_command(output_format: str, json_output: bool):
    """Show the current Lium account balance.

    Running pods draw on it per second at their $/h price; the platform
    debits the balance every 5 minutes.

    \b
    Examples:
      lium balance
      lium balance --format json
    """
    output_format = resolve_output_format(output_format, json_output)
    lium = Lium()
    balance = lium.balance()

    # Name the key: a balance that does not match `up`'s "insufficient balance"
    # usually means the two commands resolved different keys.
    key_config = getattr(lium, "config", None)

    if output_format == "json":
        # ``balance_usd`` is kept for existing consumers; ``balance`` + ``currency``
        # is the shape shared with the other commands.
        payload = {"balance": balance, "balance_usd": balance, "currency": "USD"}
        if key_config is not None:
            payload["api_key_fingerprint"] = key_config.api_key_fingerprint
            payload["api_key_source"] = key_config.api_key_source
        click.echo(json.dumps(payload, sort_keys=True))
        return

    ui.info(f"Current balance: {balance} USD")
    if key_config is not None:
        ui.dim(f"Account: {key_config.api_key_description}")
