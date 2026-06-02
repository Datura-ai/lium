"""Self-serve stablecoin top-up commands (TMC Pay).

These let an agent (or user) fund their own Lium balance with a stablecoin:
create an invoice, read the deposit address + exact crypto amount, then send
the funds from their own wallet. The balance is credited automatically once the
provider confirms the transfer — the transfer itself happens outside Lium.
"""

import json

import click

from lium.sdk import Lium, LiumError
from lium.cli import ui
from lium.cli.utils import handle_errors


@click.group("topup")
def topup_command():
    """Top up your Lium balance with a stablecoin.

    \b
    Examples:
      lium topup currencies
      lium topup create -a 20 -c USDT -n tron
    """


@topup_command.command("currencies")
@click.option("--refresh", is_flag=True, help="Bypass the cache and re-fetch")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def currencies_command(refresh: bool, json_output: bool):
    """List supported stablecoins and networks.

    \b
    Examples:
      lium topup currencies
      lium topup currencies --json
    """
    currencies = Lium().topup_currencies(refresh=refresh)

    # Treat an empty list as an error in both modes: a caller (human or agent)
    # cannot top up with no supported currency. Raising routes it through
    # handle_errors, which emits a JSON error envelope (non-zero exit) under
    # --json and a readable message otherwise — instead of silently printing
    # `{"currencies": []}` that an agent would mistake for success.
    if not currencies:
        raise LiumError("No supported currencies returned")

    if json_output:
        click.echo(json.dumps({"currencies": currencies}, sort_keys=True))
        return

    rows = [
        [c.get("code", ""), c.get("network", ""), str(c.get("display_decimals", ""))]
        for c in currencies
    ]
    ui.table(["Currency", "Network", "Decimals"], rows)


@topup_command.command("create")
@click.option("--amount", "-a", type=float, required=True, help="Top-up amount in USD")
@click.option("--currency", "-c", required=True, help="Stablecoin code, e.g. USDT")
@click.option("--network", "-n", required=True, help="Network, e.g. tron")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def create_command(amount: float, currency: str, network: str, json_output: bool):
    """Create a top-up invoice and print its deposit address.

    Send exactly the returned crypto_amount of the currency to the deposit
    address on the given network. The balance is credited automatically once
    the transfer is confirmed.

    \b
    Examples:
      lium topup create -a 20 -c USDT -n tron
      lium topup create -a 20 -c USDT -n tron --json
    """
    invoice = Lium().topup_create_invoice(
        amount=amount, crypto_currency=currency, crypto_network=network
    )

    if json_output:
        click.echo(json.dumps(invoice, sort_keys=True))
        return

    ui.success("Invoice created")
    ui.info(f"Invoice ID:      {invoice.get('invoice_id', '')}")
    ui.info(f"Deposit address: {invoice.get('deposit_address', '')}")
    ui.info(
        f"Send:            {invoice.get('crypto_amount', '')} "
        f"{invoice.get('crypto_currency', '')} on {invoice.get('crypto_network', '')}"
    )
    ui.info(
        f"For:             ${invoice.get('fiat_amount', '')} "
        f"{invoice.get('fiat_currency', 'USD')}"
    )
    if invoice.get("expires_at"):
        ui.dim(f"Expires at:      {invoice.get('expires_at')}")
    if invoice.get("hosted_invoice_url"):
        ui.dim(f"Hosted page:     {invoice.get('hosted_invoice_url')}")
