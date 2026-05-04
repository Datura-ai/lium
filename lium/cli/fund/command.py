"""Fund account commands."""

import json
from dataclasses import asdict
from typing import Optional

import click
from rich.prompt import Prompt

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors
from lium.cli.settings import config
from . import validation
from .actions import (
    LoadWalletAction,
    CheckWalletRegistrationAction,
    ExecuteTransferAction,
)


def _print_json(payload: dict) -> None:
    click.echo(json.dumps(payload, sort_keys=True))


def _legacy_tao_fund(wallet: Optional[str], amount: Optional[str], yes: bool) -> None:
    """Run the original Bittensor TAO funding flow."""
    try:
        import bittensor as bt
    except ImportError:
        ui.error("Bittensor library not installed")
        ui.dim("Install with: pip install bittensor")
        return

    if not wallet:
        default_wallet = config.get("funding.default_wallet", "default")
        wallet_name = Prompt.ask(
            "Bittensor wallet name", default=default_wallet
        ).strip()
    else:
        wallet_name = wallet

    action = LoadWalletAction()
    result = action.execute({"bt": bt, "wallet_name": wallet_name})

    if not result.ok:
        ui.error(f"Failed to load wallet '{wallet_name}': {result.error}")
        return

    bt_wallet = result.data["wallet"]
    wallet_address = result.data["address"]
    lium = Lium()

    ctx = {
        "lium": lium,
        "wallet_address": wallet_address,
        "bt_wallet": bt_wallet,
    }

    action = CheckWalletRegistrationAction()
    result = ui.load("Checking wallet registration", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(f"Failed to register wallet: {result.error}")
        return

    if not amount:
        amount_str = Prompt.ask("Enter TAO amount to fund").strip()
    else:
        amount_str = amount

    tao_amount, error = validation.validate_amount(amount_str)
    if error:
        ui.error(error)
        return

    current_balance = ui.load("Loading balance", lambda: lium.balance())
    ui.info(f"Current balance: {current_balance} USD")

    if not yes and not ui.confirm(
        f"Fund account with {tao_amount} TAO?", default=False
    ):
        return

    ui.info("Waiting for bittensor...")
    ctx = {
        "bt": bt,
        "bt_wallet": bt_wallet,
        "tao_amount": tao_amount,
    }

    action = ExecuteTransferAction()
    result = action.execute(ctx)

    if not result.ok:
        ui.error(f"Transfer failed: {result.error}")
        return

    ui.info("Done.")


@click.group("fund", invoke_without_command=True)
@click.option("--wallet", "-w", help="Bittensor wallet name for legacy TAO funding")
@click.option(
    "--amount", "-a", help="Amount of TAO to fund with legacy Bittensor funding"
)
@click.option("--yes", "-y", is_flag=True, help="Skip legacy TAO confirmation prompts")
@click.pass_context
@handle_errors
def fund_command(
    ctx: click.Context, wallet: Optional[str], amount: Optional[str], yes: bool
):
    """Fund your Lium account.

    Running this command without a subcommand keeps the legacy TAO funding flow.
    Use `lium fund crypto` to create NowPayments crypto invoices. The crypto
    commands print payment instructions only; they never read private keys or
    send crypto from your wallet.

    \b
    Legacy TAO examples:
      lium fund
      lium fund -w default -a 1.5
      lium fund -w mywal -a 0.5 -y

    \b
    Crypto examples for agents:
      lium fund crypto currencies --json
      lium fund crypto invoice --amount-usd 25 --currency usdttrc20 --json
      lium balance --json
    """
    if ctx.invoked_subcommand is None:
        _legacy_tao_fund(wallet, amount, yes)


@click.group("crypto")
def crypto_command():
    """Create NowPayments crypto funding invoices.

    These commands are designed for humans and AI agents. They provide the
    exact address, amount, currency, network, optional memo/tag, and invoice URL
    needed to pay with external wallet tooling. Lium CLI does not handle wallet
    private keys and does not send crypto.
    """


@crypto_command.command("currencies")
@click.option(
    "--refresh", is_flag=True, help="Refresh the backend NowPayments currency cache"
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def crypto_currencies_command(refresh: bool, json_output: bool):
    """List currencies supported by NowPayments.

    \b
    Examples:
      lium fund crypto currencies
      lium fund crypto currencies --refresh
      lium fund crypto currencies --json
    """
    lium = Lium()
    currencies = lium.nowpayments_currencies(refresh=refresh)

    if json_output:
        _print_json({"currencies": [asdict(currency) for currency in currencies]})
        return

    if not currencies:
        ui.warning("No NowPayments currencies returned")
        return

    rows = [
        [currency.code, currency.name or "-", currency.network or "-"]
        for currency in currencies
    ]
    ui.table(["Code", "Name", "Network"], rows)


@crypto_command.command("invoice")
@click.option("--amount-usd", required=True, help="USD balance amount to fund")
@click.option(
    "--currency",
    required=True,
    help="NowPayments pay currency code, for example usdttrc20",
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def crypto_invoice_command(amount_usd: str, currency: str, json_output: bool):
    """Create a NowPayments invoice and print payment instructions.

    After creating the invoice, send exactly `target_amount` of
    `target_currency` on the printed network to `target_address` using external
    wallet tooling. If `payin_extra_id` is present, include it as the required
    memo, tag, or destination identifier. Poll `lium balance --json` after the
    payment is confirmed to verify that the Lium balance increased.

    \b
    Examples:
      lium fund crypto invoice --amount-usd 25 --currency usdttrc20
      lium fund crypto invoice --amount-usd 25 --currency usdttrc20 --json
    """
    amount, amount_error = validation.validate_usd_amount(amount_usd)
    if amount_error:
        ui.error(amount_error)
        return

    pay_currency, currency_error = validation.validate_currency(currency)
    if currency_error:
        ui.error(currency_error)
        return

    lium = Lium()
    invoice = lium.create_nowpayments_invoice(
        amount_usd=amount,
        pay_currency=pay_currency,
    )
    payload = asdict(invoice)
    payload["amount_usd"] = amount
    payload["pay_currency"] = pay_currency

    if json_output:
        _print_json(payload)
        return

    ui.info(f"Invoice URL: {invoice.invoice_url}")
    ui.info(f"Payment ID: {invoice.payment_id}")
    ui.info(f"Status: {invoice.payment_status}")
    ui.info(f"Send: {invoice.target_amount} {invoice.target_currency}")
    if invoice.network:
        ui.info(f"Network: {invoice.network}")
    ui.info(f"Address: {invoice.target_address}")
    if invoice.payin_extra_id:
        ui.warning(f"Memo/tag required: {invoice.payin_extra_id}")
    if invoice.expires_at:
        ui.info(f"Expires at: {invoice.expires_at}")
    ui.dim("Lium CLI does not send crypto or read private keys.")
    ui.dim("After sending, poll: lium balance --json")


fund_command.add_command(crypto_command)
