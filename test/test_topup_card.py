"""P235: `lium topup card` and `Lium.topup_card` — a card top-up with no browser.

The route is lium-platform's `POST /payments/topup` (#633, behind `API_CARD_TOPUP_ENABLED`). The
SDK tests record its answers with `responses`: the 200, the 202 `processing`, the three 402s a
caller must act on (`CARD_AUTHENTICATION_REQUIRED`, `CARD_DECLINED`, `NO_SAVED_CARD`), a 402 of
another code, the 403 a key without the `billing` scope gets, and the lost answer (timeout, 5xx)
that must never read as "nothing happened". The CLI tests fake the SDK: what is printed, the
exit codes, and that a failed balance read after a successful charge is not a failure (a caller
would retry it — and charge twice).
"""

import json

import pytest
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.topup import command as topup_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_PERMISSION_DENIED
from lium.sdk import (
    Config,
    Lium,
    LiumCardTopUpError,
    LiumChargeOutcomeUnknownError,
    LiumError,
    LiumPermissionError,
)

BASE = "https://lium.io/api"
TOPUP = f"{BASE}/payments/topup"
BILLING = "https://lium.io/billing"

CHARGED = {
    "status": "succeeded",
    "payment_intent_id": "pi_3Test",
    "transaction_id": "0c7e301f-9826-4258-a13d-d82f98fd1339",
    "idempotency_key": "nightly-1",
    "amount_usd": 50.0,
    "card": {"brand": "visa", "last4": "4242"},
}
PROCESSING = {**CHARGED, "status": "processing", "payment_intent_id": None}
# the platform's 403 for a key without the scope: utils/auth.py require_api_key_scope, classified `forbidden`
# by errors/codes.py


def _error_body(status_code, detail, hint=""):
    # lium-platform core/exception_handlers.py error_response: `error` carries the code, `message` the raw detail
    return {
        "success": False,
        "error": {"code": detail["code"], "message": detail["message"], "hint": hint, "request_id": "req-1"},
        "message": detail,
        "status_code": status_code,
        "request_id": "req-1",
    }


AUTHENTICATION_REQUIRED = _error_body(
    402,
    {
        "code": "CARD_AUTHENTICATION_REQUIRED",
        "message": "Your bank asks for a confirmation this API path cannot show. Top up once by card in the "
        "dashboard (that confirms and saves the card), then retry.",
        "status": "requires_action",
        "payment_intent_id": "pi_3Auth",
        "dashboard_url": BILLING,
    },
    hint=f"Top up once by card at {BILLING} — that confirms and saves the card — then retry.",
)
DECLINED = _error_body(
    402,
    {
        "code": "CARD_DECLINED",
        "message": "Your card has insufficient funds.",
        "status": "failed",
        "decline_code": "insufficient_funds",
        "payment_intent_id": "pi_3Declined",
        "dashboard_url": BILLING,
    },
    hint=f"The bank declined the card (decline_code says why); use another saved card or fix it at {BILLING}.",
)
NO_SAVED_CARD = _error_body(
    402,
    {
        "code": "NO_SAVED_CARD",
        "message": "No card is saved on this account. Add one, or top up once by card, in the dashboard.",
        "dashboard_url": BILLING,
    },
)
# the platform's 403 for a key without the scope: `require_api_key_scope` (utils/auth.py), which
# errors/codes.py classifies as `forbidden`
SCOPE_MISSING_MESSAGE = "API key 'agent' does not have the 'billing' scope"
SCOPE_MISSING = _error_body(403, {"code": "forbidden", "message": SCOPE_MISSING_MESSAGE})


@pytest.fixture
def client():
    return Lium(Config(api_key="test-key"))


# -- SDK


@responses.activate
def test_topup_card_posts_the_body_and_returns_the_charge(client):
    responses.post(TOPUP, json=CHARGED)

    result = client.topup_card(50, payment_method_id="pm_1Card", idempotency_key="nightly-1")

    assert result == CHARGED
    request = responses.calls[0].request
    assert request.headers["X-API-KEY"] == "test-key"
    assert json.loads(request.body) == {"amount_usd": 50, "payment_method_id": "pm_1Card", "idempotency_key": "nightly-1"}


@responses.activate
def test_topup_card_always_sends_an_idempotency_key_and_omits_the_card_it_was_not_given(client):
    """No key from the caller: the SDK makes one, so a lost answer can be repeated as a replay and never as a
    second charge. The server picks the default card: no `null`s that a strict body validator refuses."""
    responses.post(TOPUP, json=CHARGED)

    client.topup_card(50)

    body = json.loads(responses.calls[0].request.body)
    assert set(body) == {"amount_usd", "idempotency_key"}
    assert body["amount_usd"] == 50
    assert len(body["idempotency_key"]) == 32 and int(body["idempotency_key"], 16) >= 0


@responses.activate
def test_a_202_processing_is_returned_not_raised(client):
    """The platform took the charge and its outcome is pending (Stripe's answer to it was lost, or the network has
    not settled): the balance settles by webhook, so this is an answer, not an error."""
    responses.post(TOPUP, json=PROCESSING, status=202)

    result = client.topup_card(50, idempotency_key="nightly-1")

    assert result == PROCESSING
    assert len(responses.calls) == 1


@responses.activate
def test_authentication_required_is_a_card_topup_error_with_the_dashboard_url(client):
    responses.post(TOPUP, json=AUTHENTICATION_REQUIRED, status=402)

    with pytest.raises(LiumCardTopUpError) as raised:
        client.topup_card(50)

    error = raised.value
    assert error.code == "CARD_AUTHENTICATION_REQUIRED"
    assert error.status == "requires_action"
    assert error.dashboard_url == BILLING
    assert error.payment_intent_id == "pi_3Auth"
    assert error.decline_code is None
    assert error.hint == AUTHENTICATION_REQUIRED["error"]["hint"]
    assert error.request_id == "req-1"
    assert "Your bank asks for a confirmation" in str(error)


@responses.activate
def test_a_decline_carries_the_banks_decline_code(client):
    responses.post(TOPUP, json=DECLINED, status=402)

    with pytest.raises(LiumCardTopUpError) as raised:
        client.topup_card(50)

    assert raised.value.code == "CARD_DECLINED"
    assert raised.value.status == "failed"
    assert raised.value.decline_code == "insufficient_funds"
    assert str(raised.value) == "Your card has insufficient funds."


@responses.activate
def test_no_saved_card_is_a_card_topup_error_too(client):
    responses.post(TOPUP, json=NO_SAVED_CARD, status=402)

    with pytest.raises(LiumCardTopUpError) as raised:
        client.topup_card(50)

    assert raised.value.code == "NO_SAVED_CARD"
    assert raised.value.dashboard_url == BILLING
    assert raised.value.status is None


@responses.activate
def test_a_402_of_another_code_stays_a_plain_lium_error(client):
    """A per-key spend cap (lium-platform#621) also answers 402; it is not a card failure."""
    responses.post(
        TOPUP, status=402, json=_error_body(402, {"code": "spend_cap_reached", "message": "This key's 24 h cap is spent."})
    )

    with pytest.raises(LiumError) as raised:
        client.topup_card(50)

    assert type(raised.value) is LiumError
    assert raised.value.code == "spend_cap_reached"


@responses.activate
def test_a_key_without_the_billing_scope_is_a_permission_error(client):
    responses.post(TOPUP, status=403, json=SCOPE_MISSING)

    with pytest.raises(LiumPermissionError) as raised:
        client.topup_card(50)

    assert raised.value.code == "forbidden"
    assert SCOPE_MISSING_MESSAGE in str(raised.value)


@responses.activate
def test_a_server_error_is_sent_once_and_is_an_unknown_outcome_carrying_the_key(client):
    """A 5xx after the POST may follow a charge Stripe already made: the SDK sends it exactly once and says the
    outcome is unknown — never "nothing happened" — with the key that makes a repeat a replay."""
    responses.post(TOPUP, status=502, json={"detail": "Stripe refused the request"})

    with pytest.raises(LiumChargeOutcomeUnknownError) as raised:
        client.topup_card(50, idempotency_key="nightly-1")

    assert len(responses.calls) == 1
    assert raised.value.idempotency_key == "nightly-1"
    assert raised.value.code == "charge_outcome_unknown"
    assert "may have gone through" in str(raised.value)


@responses.activate
def test_a_read_timeout_is_an_unknown_outcome_carrying_the_generated_key(client):
    import requests

    responses.post(TOPUP, body=requests.exceptions.ReadTimeout("Read timed out"))

    with pytest.raises(LiumChargeOutcomeUnknownError) as raised:
        client.topup_card(50)

    assert len(responses.calls) == 1
    sent = json.loads(responses.calls[0].request.body)["idempotency_key"]
    assert raised.value.idempotency_key == sent and len(sent) == 32
    assert raised.value.__cause__.__class__ is requests.exceptions.ReadTimeout


# -- CLI


class _FakeLium:
    """The SDK as the command sees it: one recorded answer for ``topup_card`` and one for ``balance``."""

    calls: list = []
    charge = CHARGED
    balance_value = 61.25

    def __init__(self):
        type(self).calls = []

    def topup_card(self, amount_usd, payment_method_id=None, idempotency_key=None):
        type(self).calls.append((amount_usd, payment_method_id, idempotency_key))
        if isinstance(self.charge, Exception):
            raise self.charge
        return self.charge

    def balance(self):
        if isinstance(self.balance_value, Exception):
            raise self.balance_value
        return self.balance_value


def _card_error(body):
    detail = body["message"]
    return LiumCardTopUpError(
        detail["message"],
        code=detail["code"],
        hint=body["error"]["hint"] or None,
        request_id="req-1",
        status=detail.get("status"),
        decline_code=detail.get("decline_code"),
        dashboard_url=detail.get("dashboard_url"),
        payment_intent_id=detail.get("payment_intent_id"),
    )


@pytest.fixture
def fake_lium(monkeypatch):
    fake = type("FakeLium", (_FakeLium,), {})
    monkeypatch.setattr(topup_module, "Lium", fake)
    return fake


def _run(*args):
    return CliRunner().invoke(cli, ["topup", "card", *args])


def _text(result):
    # Rich wraps the human rendering at the runner's 80 columns; one line for the assertions
    return " ".join(result.output.split())


def test_card_charges_and_prints_the_card_and_the_balance(fake_lium):
    result = _run("-a", "50")

    assert result.exit_code == 0, result.output
    assert "Charged $50.00 to Visa ····4242" in result.output
    assert "pi_3Test" in result.output
    assert "Idempotency key: nightly-1" in result.output
    assert "Balance:         $61.25" in result.output
    assert "'lium balance' shows it" in _text(result)
    assert fake_lium.calls == [(50.0, None, None)]


def test_card_passes_the_named_card_and_the_idempotency_key(fake_lium):
    result = _run("--amount", "25", "--card", "pm_1Card", "--idempotency-key", "nightly-2026-09-21")

    assert result.exit_code == 0, result.output
    assert fake_lium.calls == [(25.0, "pm_1Card", "nightly-2026-09-21")]


def test_card_json_is_the_servers_answer_plus_the_balance(fake_lium):
    result = _run("-a", "50", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {**CHARGED, "balance": 61.25}
    assert result.stderr == ""


def test_card_processing_is_payment_accepted_exit_0(fake_lium):
    """The platform answered 202: the charge is in, the balance follows by webhook. A success — a warning or a
    non-zero exit would invite a second run and a second charge."""
    fake_lium.charge = PROCESSING

    human = _run("-a", "50")
    assert human.exit_code == 0, human.output
    assert "Payment accepted; your balance updates within a minute" in _text(human)
    assert "Payment intent:" not in human.output  # none yet
    assert "Idempotency key: nightly-1" in human.output

    machine = _run("-a", "50", "--json")
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout) == {**PROCESSING, "balance": 61.25}


def test_a_lost_answer_says_the_charge_may_have_gone_through_and_exits_6(fake_lium):
    """A timeout or 5xx after the charge was posted: not exit 3 ("retry in a minute") nor exit 1 ("re-run with
    LIUM_DEBUG=1") — both read as "run it again", and a second run is a second charge."""
    fake_lium.charge = LiumChargeOutcomeUnknownError(
        "The charge may have gone through: the API did not answer after the top-up was posted.",
        idempotency_key="8d5b4f1c0e2a4c7f9a3b6d1e2f4a5b6c",
        code="charge_outcome_unknown",
    )

    human = _run("-a", "50")
    assert human.exit_code == EXIT_PERMISSION_DENIED, human.output
    text = _text(human)
    assert "The charge may have gone through. Check your balance with `lium balance` before trying again." in text
    assert "--idempotency-key 8d5b4f1c0e2a4c7f9a3b6d1e2f4a5b6c" in text
    for retry_wording in ("Re-run with LIUM_DEBUG", "retry in a minute", "re-run", "Retry"):
        assert retry_wording not in text
    assert "Charged" not in human.output

    machine = _run("-a", "50", "--json")
    assert machine.exit_code == EXIT_PERMISSION_DENIED, machine.output
    assert machine.stdout == ""
    payload = json.loads(machine.stderr)
    assert payload["error"]["code"] == "charge_outcome_unknown"
    assert payload["error"]["exit_code"] == EXIT_PERMISSION_DENIED
    assert payload["error"]["message"] == (
        "The charge may have gone through. Check your balance with `lium balance` before trying again."
    )
    assert payload["data"] == {"idempotency_key": "8d5b4f1c0e2a4c7f9a3b6d1e2f4a5b6c"}
    assert "--idempotency-key 8d5b4f1c0e2a4c7f9a3b6d1e2f4a5b6c" in payload["error"]["hint"]
    assert fake_lium.calls == [(50.0, None, None)]


def test_a_failed_balance_read_after_the_charge_is_not_a_failure(fake_lium):
    """The charge went through; exiting non-zero would invite a retry — and a second charge."""
    fake_lium.balance_value = LiumError("Server error: 503")

    human = _run("-a", "50")
    assert human.exit_code == 0, human.output
    assert "Charged $50.00" in human.output
    assert "Balance:" not in human.output

    machine = _run("-a", "50", "--json")
    assert machine.exit_code == 0, machine.output
    assert json.loads(machine.stdout)["balance"] is None


def test_authentication_required_names_the_billing_page_and_exits_3(fake_lium):
    fake_lium.charge = _card_error(AUTHENTICATION_REQUIRED)

    result = _run("-a", "50")

    assert result.exit_code == EXIT_API_ERROR
    text = _text(result)
    assert "Your bank asks for a confirmation this API path cannot show" in text
    assert f"Billing page: {BILLING}" in text
    assert "confirms and saves the card" in text  # the hint
    assert "Charged" not in result.output


def test_authentication_required_json_envelope_carries_the_structured_fields(fake_lium):
    fake_lium.charge = _card_error(AUTHENTICATION_REQUIRED)

    result = _run("-a", "50", "--json")

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "CARD_AUTHENTICATION_REQUIRED"
    assert payload["error"]["exit_code"] == EXIT_API_ERROR
    assert payload["error"]["hint"] == AUTHENTICATION_REQUIRED["error"]["hint"]
    assert payload["data"] == {
        "status": "requires_action",
        "dashboard_url": BILLING,
        "payment_intent_id": "pi_3Auth",
        "request_id": "req-1",
    }


def test_a_decline_prints_the_banks_reason(fake_lium):
    fake_lium.charge = _card_error(DECLINED)

    result = _run("-a", "50", "--json")

    assert result.exit_code == EXIT_API_ERROR
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "CARD_DECLINED"
    assert payload["error"]["message"] == (
        f"Your card has insufficient funds (decline_code: insufficient_funds). Billing page: {BILLING}"
    )
    assert payload["data"]["decline_code"] == "insufficient_funds"


def test_an_older_server_without_a_hint_gets_the_clis_own(fake_lium):
    fake_lium.charge = _card_error(NO_SAVED_CARD)

    result = _run("-a", "50", "--json")

    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "NO_SAVED_CARD"
    assert payload["error"]["hint"] == topup_module._CARD_HINTS["NO_SAVED_CARD"]


def test_a_key_without_the_billing_scope_exits_6(fake_lium):
    # what the SDK raises for SCOPE_MISSING (test_a_key_without_the_billing_scope_is_a_permission_error)
    fake_lium.charge = LiumPermissionError(f"Permission denied: {SCOPE_MISSING_MESSAGE}", code="forbidden")

    result = _run("-a", "50", "--json")

    assert result.exit_code == EXIT_PERMISSION_DENIED
    payload = json.loads(result.stderr)
    assert payload["error"]["code"] == "forbidden"
    assert SCOPE_MISSING_MESSAGE in payload["error"]["message"]
    assert fake_lium.calls == [(50.0, None, None)]


def test_amount_is_required():
    result = _run()

    assert result.exit_code == 2
    assert "--amount" in result.output
