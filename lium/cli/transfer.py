"""`lium transfer`: move credit to another Lium account."""

import json
import math
from typing import Optional

import click
from rich.markup import escape

from lium.sdk import LiumScopeError
from lium.cli import ui
from lium.cli.topup.command import BILLING_KEY_ENV_VAR, billing_client
from lium.cli.utils import EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED, CliFailure, handle_errors

BILLING_KEY_HINT = ("Transfers need an API key with the billing scope: 'lium workspaces login', then "
                    f"'lium keys create payer --scope billing', and export the key as {BILLING_KEY_ENV_VAR}")


@click.command("transfer")
@click.argument("to", metavar="EMAIL|USER_ID")
@click.argument("amount", type=float)
@click.option("--note", default=None, metavar="TEXT", help="A note the recipient sees")
@click.option("--yes", "-y", is_flag=True, help="Transfer without asking first (required with --json)")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def transfer_command(to: str, amount: float, note: Optional[str], yes: bool, json_output: bool):
    """Send AMOUNT USD of your balance to another account, by e-mail or account id.

    Asks first; `--yes` skips the question and `--json` needs it. The API key must hold the
    `billing` scope. Errors exit non-zero with the API's code: self_transfer (sending to
    yourself), insufficient_balance, recipient_unavailable (no such account).

    \b
    Examples:
      lium transfer friend@example.com 25
      lium transfer 3f1c...-uuid 10 --note "GPU hours" --yes --json
    """
    if not math.isfinite(amount) or amount <= 0:
        raise CliFailure("invalid_arguments", "AMOUNT must be a positive number of dollars.", EXIT_CONFIGURATION_ERROR)
    question = f"Transfer ${amount:,.2f} to {to}? This cannot be undone."
    if not yes:
        if json_output:
            raise CliFailure("confirmation_required",
                             f"Confirmation required: this sends ${amount:,.2f} to {to}. Pass --yes with --json.",
                             EXIT_CONFIGURATION_ERROR)
        if not ui.confirm(question, default=False):
            ui.info("Nothing transferred.")
            return

    try:
        result = billing_client().transfer_credits(to, amount, description=note)
    except LiumScopeError as e:
        raise CliFailure("missing_scope", str(e), EXIT_PERMISSION_DENIED,
                         data={"scope": e.scope or "billing", **({"request_id": e.request_id} if e.request_id else {})},
                         hint=BILLING_KEY_HINT)

    if json_output:
        click.echo(json.dumps({**result, "to": to, "amount": amount}, sort_keys=True))
        return
    ui.success(f"Sent ${amount:,.2f} to {escape(to)}")
    if result.get("transfer_id"):
        ui.info(f"Transfer id: {escape(str(result['transfer_id']))}")
    if result.get("sender_balance") is not None:
        ui.info(f"Balance:     ${result['sender_balance']:,.2f}")
