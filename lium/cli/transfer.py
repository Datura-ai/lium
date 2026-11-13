"""`lium transfer`: move credit to another Lium account."""

import json
import math
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

import click
from rich.markup import escape

from lium.sdk import LiumChargeOutcomeUnknownError, LiumError, LiumScopeError
from lium.cli import ui
from lium.cli.topup.command import BILLING_KEY_ENV_VAR, billing_client
from lium.cli.utils import (
    EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED, CliFailure, _api_error_data, _classify_sdk_error, handle_errors,
)

BILLING_KEY_HINT = ("Transfers need an API key with the billing scope: 'lium workspaces login', then "
                    f"'lium keys create payer --scope billing', and export the key as {BILLING_KEY_ENV_VAR}")
# The generic hints for these codes say "retry" or point at nodes and pods; nothing was transferred
REFUSAL_HINTS = {
    "self_transfer": "The sender is the account that owns the billing key; send to another account's e-mail or id.",
    "recipient_unavailable": "That account cannot receive credit: no account has that e-mail or id, or it is "
                             "deleted or blocked. Check it with the recipient: the e-mail they sign in with, or "
                             "their account id.",
}


@click.command("transfer")
@click.argument("to", metavar="EMAIL|USER_ID")
@click.argument("amount", type=float)
@click.option("--note", default=None, metavar="TEXT", help="A note the recipient sees")
@click.option(
    "--idempotency-key", default=None, metavar="KEY",
    help="The Idempotency-Key header to send. Within 24 h an identical repeat of an answered transfer gets "
         "that answer back; later the key is forgotten and a repeat transfers again. Omitted, a fresh one is made",
)
@click.option("--yes", "-y", is_flag=True, help="Transfer without asking first (required with --json)")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def transfer_command(to: str, amount: float, note: Optional[str], idempotency_key: Optional[str], yes: bool,
                     json_output: bool):
    """Send AMOUNT USD of your balance to another account, by e-mail or account id.

    Asks first; `--yes` skips the question and `--json` needs it. The API key must hold the
    `billing` scope. Errors exit non-zero with the API's code: self_transfer (sending to
    yourself), insufficient_balance, recipient_unavailable (no account with that e-mail or id, or a
    deleted or blocked one). If the answer is lost (timeout, dropped connection, 5xx, Ctrl-C,
    or the same key still in progress) the transfer may have happened: exit 6 with
    transfer_outcome_unknown and the idempotency key; check transfer history on the Billing page of
    the account that owns the billing key and, if it is not there, contact support with the key.
    Do not run it again, with that key or a new one. AMOUNT is rounded to the cent as the
    API does, before the question.

    \b
    Examples:
      lium transfer friend@example.com 25
      lium transfer 3f1c...-uuid 10 --note "GPU hours" --yes --json
    """
    if not math.isfinite(amount) or amount <= 0:
        raise CliFailure("invalid_arguments", "AMOUNT must be a positive number of dollars.", EXIT_CONFIGURATION_ERROR)
    # the API's own rounding (str() first, half up): the confirmed, sent, debited and printed amounts agree
    try:
        amount = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        raise CliFailure("invalid_arguments", "AMOUNT is too large.", EXIT_CONFIGURATION_ERROR)
    if amount < 0.01:
        raise CliFailure("invalid_arguments", "AMOUNT must be at least $0.01.", EXIT_CONFIGURATION_ERROR)
    question = f"Transfer ${amount:,.2f} to {to}? This cannot be undone."
    if not yes:
        if json_output:
            raise CliFailure("confirmation_required",
                             f"Confirmation required: this sends ${amount:,.2f} to {to}. Pass --yes with --json.",
                             EXIT_CONFIGURATION_ERROR)
        if not ui.confirm(question, default=False):
            ui.info("Nothing transferred.")
            return

    key = idempotency_key or str(uuid.uuid4())  # made here so a lost answer can still name it
    try:
        result = billing_client().transfer_credits(to, amount, description=note, idempotency_key=key)
    except (LiumChargeOutcomeUnknownError, KeyboardInterrupt) as e:
        # Ctrl-C after the POST may follow a commit too. No re-run advice: a backend that frees the key on a
        # raised 500 would move the credit again
        request_id = getattr(e, "request_id", None)
        raise CliFailure(
            "transfer_outcome_unknown",
            f"The transfer may or may not have happened. Idempotency key: {key}",
            EXIT_PERMISSION_DENIED,
            data={"idempotency_key": key, "amount": amount, "to": to,
                  **({"request_id": request_id} if request_id else {})},
            hint="Do not run it again, with this key or a new one. Check transfer history on the Billing page of the "
                 "account that owns the billing key; if the transfer is not there, contact support with idempotency "
                 f"key {key}.",
        )
    except LiumScopeError as e:
        raise CliFailure("missing_scope", str(e), EXIT_PERMISSION_DENIED,
                         data={"scope": e.scope or "billing", **({"request_id": e.request_id} if e.request_id else {})},
                         hint=BILLING_KEY_HINT)
    except LiumError as e:
        if e.hint or e.code not in REFUSAL_HINTS:
            raise
        raise CliFailure(e.code, str(e), _classify_sdk_error(e)[1], data=_api_error_data(e),
                         hint=REFUSAL_HINTS[e.code]) from e

    if json_output:
        click.echo(json.dumps({**result, "to": to, "amount": amount}, sort_keys=True))
        return
    ui.success(f"Sent ${amount:,.2f} to {escape(to)}")
    if result.get("transfer_id"):
        ui.info(f"Transfer id: {escape(str(result['transfer_id']))}")
    ui.info(f"Idempotency key: {escape(str(result.get('idempotency_key') or key))}")
    if result.get("sender_balance") is not None:
        ui.info(f"Balance:     ${result['sender_balance']:,.2f}")
