"""Self-serve stablecoin top-up commands (TMC Pay).

These let an agent (or user) fund their own Lium balance with a stablecoin:
create an invoice, read the deposit address + exact crypto amount, then send
the funds from their own wallet. The balance is credited automatically once the
provider confirms the transfer — the transfer itself happens outside Lium.
"""

import json
import math
import os
from typing import Optional

import click
from rich.markup import escape

from lium.sdk import Lium, LiumCardTopUpError, LiumChargeOutcomeUnknownError, LiumError
from lium.sdk.config import Config
from lium.cli import ui
from lium.cli.settings import config
from lium.cli.utils import (
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_PERMISSION_DENIED,
    CliFailure,
    handle_errors,
)

BILLING_KEY_ENV_VAR = "LIUM_BILLING_API_KEY"
BILLING_KEY_OPTION = "api.billing_api_key"
WAIT_HELP = ("Wait up to SECONDS for the balance to rise (the payment provider's webhook credits it), "
             "then print how long it took; under --json the payment details go to stderr first")


def billing_client() -> Lium:
    """The client for money routes: the `billing` key when one is set (LIUM_BILLING_API_KEY, then
    `[api] billing_api_key`, which `lium signup --billing-key` writes), else the usual key."""
    key = os.environ.get(BILLING_KEY_ENV_VAR)
    source = f"env:{BILLING_KEY_ENV_VAR}"
    if not key:
        key = config.get(BILLING_KEY_OPTION)
        source = f"config:{config.get_config_path()} [api] billing_api_key"
    if not key:
        return Lium()
    return Lium(config=Config(
        api_key=key,
        base_url=os.getenv("LIUM_BASE_URL", "https://lium.io/api"),
        base_pay_url=os.getenv("LIUM_PAY_URL", "https://pay-api.lium.io"),
        api_key_source=source,
    ))


def balance_client(fallback: Lium) -> Lium:
    """The usual key reads the balance; a billing key may too (GET /users/me), for an agent holding only that."""
    try:
        return Lium()
    except (LiumError, ValueError):
        return fallback


def read_balance(client: Lium) -> Optional[float]:
    # best effort: the baseline and the final figure are reports, never a reason to fail after money moved
    try:
        return client.balance()
    except Exception:
        return None


def wait_for_credit_or_fail(client: Lium, baseline: Optional[float], wait: float, extra: dict) -> dict:
    """Wait for the balance to pass `baseline`; the result dict on success, a CliFailure when time runs out."""
    if baseline is None:
        baseline = read_balance(client)
        if baseline is None:
            raise CliFailure(
                "balance_unreadable",
                "Could not read the balance to wait on. Check it with 'lium balance --json'.",
                EXIT_API_ERROR,
                data=extra,
            )
    outcome = client.wait_for_credit(baseline, timeout=wait)
    if not outcome["credited"]:
        raise CliFailure(
            "credit_not_seen",
            f"The balance did not rise above ${baseline:,.2f} within {wait:g} s. If the payment was made, "
            f"run 'lium topup wait --above {baseline:.2f}' to keep waiting; check 'lium balance --json' "
            "before paying again.",
            EXIT_API_ERROR,
            data={**extra, "balance_before": baseline, "balance": outcome["balance"], "waited_seconds": outcome["seconds"]},
        )
    return {"credited": True, "balance_before": baseline, "balance": outcome["balance"],
            "seconds_to_credit": outcome["seconds"]}


def announce(payload: dict, json_output: bool) -> None:
    """Under --json with --wait: the payment details on stderr now, so an agent can act on them while
    the command waits; stdout keeps its one final document."""
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True), err=True)


def check_wait(wait: Optional[float]) -> None:
    if wait is not None and (not math.isfinite(wait) or wait <= 0):
        raise CliFailure("invalid_arguments", "--wait must be a positive number of seconds.", EXIT_CONFIGURATION_ERROR)

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
      lium topup link -a 10 --wait 900
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
@click.option("--wait", type=float, default=None, metavar="SECONDS", help=WAIT_HELP)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def create_command(amount: float, currency: str, network: str, wait: float | None, json_output: bool):
    """Create a top-up invoice and print its deposit address.

    Send exactly the returned crypto_amount of the currency to the deposit
    address on the given network. The balance is credited automatically once
    the transfer is confirmed.

    With `--wait SECONDS` the command stays up until the credit lands: the invoice
    is printed first (on stderr under --json, as one JSON line), then the final
    document adds `seconds_to_credit`. Time out: exit 3, credit_not_seen.

    \b
    Examples:
      lium topup create -a 20 -c USDT -n tron
      lium topup create -a 20 -c USDC -n base --json
      lium topup create -a 20 -c USDC -n base --wait 900 --json
    """
    check_wait(wait)
    client = billing_client()
    reader = balance_client(client)
    baseline = read_balance(reader) if wait else None
    invoice = client.topup_create_invoice(
        amount=amount, crypto_currency=currency, crypto_network=network
    )

    if wait:
        announce({**invoice, "event": "invoice_created"}, json_output)
        if not json_output:
            print_invoice(invoice)
            ui.info(f"Waiting up to {wait:g} s for the transfer to be credited…")
        credit = wait_for_credit_or_fail(reader, baseline, wait, {"invoice_id": invoice.get("invoice_id")})
        if json_output:
            click.echo(json.dumps({**invoice, **credit}, sort_keys=True))
        else:
            ui.success(f"Credited in {credit['seconds_to_credit']:g} s — balance ${credit['balance']:,.2f}")
        return

    if json_output:
        click.echo(json.dumps(invoice, sort_keys=True))
        return

    print_invoice(invoice)


def print_invoice(invoice: dict) -> None:
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


@topup_command.command("link")
@click.option("--amount", "-a", type=float, required=True, help="Top-up amount in USD (at least $10)")
@click.option("--wait", type=float, default=None, metavar="SECONDS", help=WAIT_HELP)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def link_command(amount: float, wait: float | None, json_output: bool):
    """Make a card payment page for a person to pay; nothing is charged by this command.

    For an agent with no card of its own: it hands the printed `url` (Stripe Checkout) to
    a person, who enters a card there — and passes the bank's 3-D Secure check if it asks.
    The balance is credited by Stripe's webhook seconds after the payment; the card is saved
    on the account. The key must hold the `billing` scope: LIUM_BILLING_API_KEY, else
    `[api] billing_api_key` (written by `lium signup --billing-key`), else the usual key.

    With `--wait SECONDS` the command waits for the credit: the link is printed first (on
    stderr under --json, as one JSON line with `"event": "handoff"`), and the final document
    adds `seconds_to_credit`. Time out: exit 3, credit_not_seen — `lium topup wait` resumes.

    \b
    Examples:
      lium topup link -a 10
      lium topup link -a 10 --json
      lium topup link -a 10 --wait 900 --json
    """
    if not math.isfinite(amount) or amount <= 0:
        raise CliFailure("invalid_arguments", "--amount must be a positive number of dollars (at least 10).",
                         EXIT_CONFIGURATION_ERROR)
    check_wait(wait)
    client = billing_client()
    reader = balance_client(client)
    baseline = read_balance(reader)
    link = client.topup_checkout_link(amount)
    payload = {
        **link,
        "balance_before": baseline,
        "handoff": {
            "step": "card_payment",
            "who": "a person with a card",
            "what": f"open the url and pay ${amount:,.2f}; the balance is credited seconds after",
        },
    }

    if wait:
        announce({**payload, "event": "handoff"}, json_output)
        if not json_output:
            ui.info(f"Pay here: {link['url']}")
            ui.info(f"Waiting up to {wait:g} s for the payment…")
        credit = wait_for_credit_or_fail(reader, baseline, wait, {"session_id": link.get("session_id")})
        if json_output:
            click.echo(json.dumps({**payload, **credit}, sort_keys=True))
        else:
            ui.success(f"Credited in {credit['seconds_to_credit']:g} s — balance ${credit['balance']:,.2f}")
        return

    if json_output:
        click.echo(json.dumps(payload, sort_keys=True))
        return
    ui.success(f"Payment page for ${amount:,.2f}")
    ui.info(f"URL: {link['url']}")
    ui.dim("Nothing is charged until someone pays there. 'lium topup wait' waits for the credit.")


@topup_command.command("wait")
@click.option("--above", type=float, default=None, metavar="USD",
              help="Wait for the balance to rise above this (default: the balance now)")
@click.option("--timeout", "timeout_s", type=float, default=600, show_default=True, metavar="SECONDS",
              help="Give up after this many seconds")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def wait_command(above: float | None, timeout_s: float, json_output: bool):
    """Wait until a top-up is on the balance.

    Pass the balance read before paying (`balance_before` from `topup link`, or
    `lium balance --json`) as --above; without it, the balance now is the baseline, so
    a credit that already landed is not seen. Exit 0 with `seconds_to_credit`, or exit 3
    with credit_not_seen when the time runs out.

    \b
    Examples:
      lium topup wait --above 0 --timeout 900 --json
    """
    check_wait(timeout_s)
    reader = balance_client(billing_client())
    credit = wait_for_credit_or_fail(reader, above, timeout_s, {})
    if json_output:
        click.echo(json.dumps(credit, sort_keys=True))
        return
    ui.success(f"Credited in {credit['seconds_to_credit']:g} s — balance ${credit['balance']:,.2f}")


@topup_command.command("card")
@click.option("--amount", "-a", type=float, required=True, help="Top-up amount in USD (at least $10)")
@click.option(
    "--card", "payment_method_id", default=None, metavar="PM_ID",
    help="A saved card's pm_… id; omitted, the default card (or the only saved one)",
)
@click.option(
    "--idempotency-key", default=None, metavar="KEY",
    help="Use a new key for each top-up. A used key and the same amount return the first charge "
         "(or its status) within 24 h instead of making a second one; after that, check "
         "`lium balance` before repeating. Omitted, one is made and printed",
)
@click.option("--yes", "-y", is_flag=True, help="Charge without asking first (required with --json)")
@click.option("--wait", type=float, default=None, metavar="SECONDS", help=WAIT_HELP)
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def card_command(amount: float, payment_method_id: str | None, idempotency_key: str | None, yes: bool,
                 wait: float | None, json_output: bool):
    """Charge a saved card and top up the balance, with no browser (not released: the platform
    switch is off).

    The card must already be saved on the account (a card top-up on the Billing page saves it).
    The command asks before charging; `--yes` skips the question, and `--json` needs it, as
    `lium fund` does: behind a pipe nobody can answer, so it fails with confirmation_required
    before anything is sent. The charge is made straight away; the balance is credited by
    Stripe's confirmation, usually within seconds, so the balance printed here may not include
    it yet. The API key must hold the `billing` scope: today's read / rent / manage keys are
    refused.

    A bank that wants a one-time confirmation (3-D Secure) cannot get one through this path:
    the command fails with CARD_AUTHENTICATION_REQUIRED and the Billing page to confirm the
    card on; a decline fails with CARD_DECLINED and the bank's reason. Nothing is charged
    in either case.

    The request is sent once, always with an idempotency key. If the answer is lost (a
    timeout, a 5xx) the charge may still have gone through: the command exits 6 with
    charge_outcome_unknown and the key — check `lium balance` before trying again; a repeat
    with `--idempotency-key <key>` and the same amount returns the same charge within 24 h
    instead of making a second one; after that, check `lium balance` before repeating. The
    same key with a different amount is a new charge. A 202 with a payment_intent_id is
    success (exit 0): the platform took the charge. A 202 without that id means the
    outcome is not known yet — exit 6 with charge_pending, the key and the amount; a
    repeat with both shows the charge's status within 24 h.

    `--wait SECONDS` keeps the command running until the credit is on the balance and adds
    `seconds_to_credit`; if the time runs out it exits 3 with credit_not_seen and the
    idempotency key — the charge was still made, so do not run the command again without it.

    \b
    Examples:
      lium topup card -a 50
      lium topup card -a 50 --yes --wait 60 --json
      lium topup card -a 50 --card pm_1Abc... --yes --json
      lium topup card -a 50 --idempotency-key nightly-2026-09-21
    """
    if not math.isfinite(amount):
        # click's float type accepts nan/inf; those never reach the API, so they must not look like
        # a lost charge (exit 6 / "may have gone through").
        raise CliFailure(
            "invalid_arguments",
            "--amount must be a finite number of dollars (at least 10).",
            EXIT_CONFIGURATION_ERROR,
        )
    check_wait(wait)

    if not yes:
        # Real money leaves a card here, so the gate is `lium fund`'s: ask, and under --json refuse
        # rather than prompt — a script reading stdout cannot answer, and an unanswered default
        # would either charge unasked or exit 0 having charged nothing.
        target = f"card {payment_method_id}" if payment_method_id else "the default saved card"
        if json_output:
            raise CliFailure(
                "confirmation_required",
                f"Confirmation required: this charges ${amount:,.2f} to {target}. Pass --yes with --json.",
                EXIT_CONFIGURATION_ERROR,
            )
        if not ui.confirm(f"Charge ${amount:,.2f} to {target}? The charge is made straight away.", default=False):
            ui.info("Nothing charged.")
            return

    client = billing_client()
    reader = balance_client(client)
    baseline = read_balance(reader) if wait else None
    if wait and baseline is None:
        raise CliFailure(
            "balance_unreadable",
            "Could not read the balance to wait on, so nothing was charged. Check 'lium balance --json', "
            "or run without --wait.",
            EXIT_API_ERROR,
        )
    try:
        result = client.topup_card(
            amount, payment_method_id=payment_method_id, idempotency_key=idempotency_key
        )
    except LiumCardTopUpError as e:
        raise card_topup_failure(e)
    except LiumChargeOutcomeUnknownError as e:
        raise charge_outcome_unknown_failure(e)

    if result.get("status") == "processing" and not result.get("payment_intent_id"):
        # 202 without a payment intent: Stripe's answer to the charge was lost; the charge may never
        # have happened. Not a success — stop so a caller does not treat it as credited. The same
        # idempotency key repeats this charge and never makes a second one.
        raise charge_pending_failure(result)

    # 200 succeeded, or 202 processing with a payment_intent_id (lium-platform#633: the platform
    # took the charge and named the intent). Exit 0. Credit lands by webhook; a repeat with the
    # same idempotency key and amount returns this charge and never a second one.

    # Best effort: the charge is done, so a balance read that fails must not turn the command
    # into a failure a caller would retry (and charge again). `balance()` re-raises a raw
    # `requests.RequestException` after its retries (not a LiumError), so this is Exception.
    credit = {}
    if wait:
        announce({**result, "event": "charged"}, json_output)
        credit = wait_for_credit_or_fail(
            reader, baseline, wait, {"idempotency_key": result.get("idempotency_key"), "amount_usd": amount}
        )
        balance = credit["balance"]
    else:
        balance = read_balance(client)

    if json_output:
        click.echo(json.dumps({**result, **credit, "balance": balance}, sort_keys=True))
        return

    card = result.get("card") or {}
    card_text = " ".join(part for part in [(card.get("brand") or "card").capitalize(),
                                           f"····{card['last4']}" if card.get("last4") else ""] if part)
    amount_usd = result.get("amount_usd", amount)
    ui.success(f"Charged ${amount_usd:,.2f} to {card_text}")
    if result.get("payment_intent_id"):
        ui.info(f"Payment intent:  {escape(str(result['payment_intent_id']))}")
    if result.get("idempotency_key"):
        ui.info(f"Idempotency key: {escape(str(result['idempotency_key']))}")
    if balance is not None:
        ui.info(f"Balance:         ${balance:,.2f}")
    if credit:
        ui.info(f"Credited in:     {credit['seconds_to_credit']:g} s")
    else:
        ui.dim("The credit lands within seconds of Stripe's confirmation; 'lium balance' shows it.")


def charge_pending_failure(result: dict) -> CliFailure:
    """A 202 ``processing`` reply with no ``payment_intent_id``: the outcome is not known (Stripe's
    answer to the charge call was lost). A 202 that includes a ``payment_intent_id`` is success
    (exit 0), not this path. The same exit 6 as a lost answer — "stop, a person must look" —
    with the key that makes a repeat safe; the repeat returns the same charge's status
    (``succeeded`` once credited, ``processing`` until then) and never makes a second charge. The
    server's answer, ``status: processing`` included, is in ``data``."""
    key = result.get("idempotency_key")
    amount = result.get("amount_usd")
    amount_text = (
        f"${amount:,.2f}" if isinstance(amount, (int, float)) and math.isfinite(amount) else "the same amount"
    )
    data = {name: result[name] for name in ("status", "idempotency_key", "transaction_id", "payment_intent_id",
                                            "amount_usd", "card") if name in result}
    return CliFailure(
        "charge_pending",
        "Payment submitted; the charge is still being confirmed. Check `lium balance` in a minute — if nothing "
        f"arrived, re-run with `--idempotency-key {key}` and the same amount ({amount_text}) to see its status.",
        EXIT_PERMISSION_DENIED,
        data=data,
        hint=f"Run 'lium balance' in a minute; 'lium topup card --idempotency-key {key}' with the same amount "
             "returns this charge's status within 24 h and does not make a second one. After that, check "
             "`lium balance` before repeating. A different amount is a new charge.",
    )


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
        hint=f"Run 'lium balance'; to repeat safely within 24 h, 'lium topup card --idempotency-key "
             f"{error.idempotency_key}' with the same amount returns the same charge instead of a second one. "
             "After that, check `lium balance` before repeating.",
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
