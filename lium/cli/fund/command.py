"""Fund account command."""

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


def _legacy_tao_fund(wallet: Optional[str], amount: Optional[str], yes: bool) -> None:
    """Run the Bittensor TAO funding flow."""
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


@click.command("fund")
@click.option("--wallet", "-w", help="Bittensor wallet name to fund from")
@click.option("--amount", "-a", help="Amount of TAO to fund with")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@handle_errors
def fund_command(wallet: Optional[str], amount: Optional[str], yes: bool):
    """Fund your Lium account with TAO from a Bittensor wallet.

    \b
    Examples:
      lium fund
      lium fund -w default -a 1.5
      lium fund -w mywal -a 0.5 -y
    """
    _legacy_tao_fund(wallet, amount, yes)
