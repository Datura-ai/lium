"""Self-serve stablecoin top-up commands (TMC Pay).

These let an agent (or user) fund their own Lium balance with a stablecoin:
create an invoice, read the deposit address + exact crypto amount, then send
the funds from their own wallet. The balance is credited automatically once the
provider confirms the transfer — the transfer itself happens outside Lium.
"""

import json

import click

from lium.sdk import Lium, LiumCardTopUpError, LiumChargeOutcomeUnknownError, LiumError
from lium.cli import ui
from lium.cli.utils import EXIT_API_ERROR, EXIT_PERMISSION_DENIED, CliFailure, handle_errors

# What to do next when the server's 402 carried no hint (an older server); the current server
# sends one naming the Billing page (lium-platform errors/codes.py) and that one wins.
_CARD_HINTS = {
    "CARD_AUTHENTICATION_REQUIRED": "Top up once by card on the Billing page (that confirms and saves "
                                    "the card), then retry",
    "CARD_DECLINED": "Use another saved card (--card <pm_id>) or fix the card on the Billing page",
    "NO_SAVED_CARD": "Add a card, or top up once by card, on the Billing page; then retry",
    "NO_DEFAULT_CARD": "Pass --card <pm_id>, or set a default card on the Billing page",
}


@click.group("topup")
def topup_command():
    """Top up your Lium balance with a stablecoin, or a saved card.

    \b
    Examples:
      lium topup currencies
      lium topup create -a 20 -c USDT -n tron
      lium topup card -a 50
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


@topup_command.command("card")
@click.option("--amount", "-a", type=float, required=True, help="Top-up amount in USD (at least $10)")
@click.option(
    "--card", "payment_method_id", default=None, metavar="PM_ID",
    help="A saved card's pm_… id; omitted, the default card (or the only saved one)",
)
@click.option(
    "--idempotency-key", default=None, metavar="KEY",
    help="Repeat the command with the same key and amount within 24 h and the first charge is "
         "returned instead of a second one being made; omitted, one is made and printed",
)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def card_command(amount: float, payment_method_id: str | None, idempotency_key: str | None, json_output: bool):
    """Charge a saved card and top up the balance, with no browser (not released: the platform
    switch is off).

    The card must already be saved on the account (a card top-up on the Billing page saves it).
    The charge is made straight away; the balance is credited by Stripe's confirmation, usually
    within seconds, so the balance printed here may not include it yet. The API key must hold
    the `billing` scope: today's read / rent / manage keys are refused.

    A bank that wants a one-time confirmation (3-D Secure) cannot get one through this path:
    the command fails with CARD_AUTHENTICATION_REQUIRED and the Billing page to confirm the
    card on; a decline fails with CARD_DECLINED and the bank's reason. Nothing is charged
    in either case.

    The request is sent once, always with an idempotency key. If the answer is lost (a
    timeout, a 5xx) the charge may still have gone through: the command exits 6 with
    charge_outcome_unknown and the key — check `lium balance` before trying again; a repeat
    with `--idempotency-key <key>` returns the same charge instead of making a second one.
    "Payment accepted" (exit 0) means the platform took the charge and the balance updates
    within a minute.

    \b
    Examples:
      lium topup card -a 50
      lium topup card -a 50 --card pm_1Abc... --json
      lium topup card -a 50 --idempotency-key nightly-2026-09-21
    """
    client = Lium()
    try:
        result = client.topup_card(
            amount, payment_method_id=payment_method_id, idempotency_key=idempotency_key
        )
    except LiumCardTopUpError as e:
        raise card_topup_failure(e)
    except LiumChargeOutcomeUnknownError as e:
        raise charge_outcome_unknown_failure(e)

    # Best effort: the charge is done, so a balance read that fails must not turn the command
    # into a failure a caller would retry (and charge again).
    try:
        balance = client.balance()
    except LiumError:
        balance = None

    if json_output:
        click.echo(json.dumps({**result, "balance": balance}, sort_keys=True))
        return

    card = result.get("card") or {}
    card_text = " ".join(part for part in [(card.get("brand") or "card").capitalize(),
                                           f"····{card['last4']}" if card.get("last4") else ""] if part)
    amount_usd = result.get("amount_usd", amount)
    if result.get("status") == "processing":
        # the platform took the charge but its outcome is not settled (or Stripe's answer to it was lost);
        # the webhook settles the balance — a success, never a prompt to run the command again
        ui.success(f"Payment accepted; your balance updates within a minute (${amount_usd:,.2f} to {card_text}).")
    else:
        ui.success(f"Charged ${amount_usd:,.2f} to {card_text}")
    if result.get("payment_intent_id"):
        ui.info(f"Payment intent:  {result['payment_intent_id']}")
    if result.get("idempotency_key"):
        ui.info(f"Idempotency key: {result['idempotency_key']}")
    if balance is not None:
        ui.info(f"Balance:         ${balance:,.2f}")
    ui.dim("The credit lands within seconds of Stripe's confirmation; 'lium balance' shows it.")


def charge_outcome_unknown_failure(error: LiumChargeOutcomeUnknownError) -> CliFailure:
    """The answer to the charge was lost (timeout, dropped connection, 5xx): Stripe charges before it
    answers, so the charge may have gone through. Exit 6 — the code scripts already treat as "stop,
    a person must look" — never 3 or 1, whose hints say to run the command again. The key that
    makes a repeat safe is in the message and in ``data``."""
    return CliFailure(
        "charge_outcome_unknown",
        "The charge may have gone through. Check your balance with `lium balance` before trying again.",
        EXIT_PERMISSION_DENIED,
        data={"idempotency_key": error.idempotency_key, **({"request_id": error.request_id} if error.request_id else {})},
        hint=f"Run 'lium balance'; to repeat safely, 'lium topup card --idempotency-key {error.idempotency_key}' "
             "with the same amount returns the same charge instead of a second one",
    )


def card_topup_failure(error: LiumCardTopUpError) -> CliFailure:
    """The failure for a 402 from ``POST /payments/topup``: the server's code (``CARD_DECLINED``, …)
    and sentence, the bank's ``decline_code`` in the text, and the structured fields — ``status``,
    ``decline_code``, ``dashboard_url``, ``payment_intent_id``, ``request_id`` — in ``data`` for a
    machine reader. Exit 3: the API refused the call, and the balance did not change."""
    message = str(error)
    if error.decline_code:
        message = f"{message.rstrip('.')} (decline_code: {error.decline_code})."
    if error.dashboard_url:
        message = f"{message} Billing page: {error.dashboard_url}"
    data = {
        key: value
        for key, value in {
            "status": error.status,
            "decline_code": error.decline_code,
            "dashboard_url": error.dashboard_url,
            "payment_intent_id": error.payment_intent_id,
            "request_id": error.request_id,
        }.items()
        if value
    }
    code = error.code or "card_topup_failed"
    return CliFailure(code, message, EXIT_API_ERROR, data=data or None, hint=error.hint or _CARD_HINTS.get(code))
