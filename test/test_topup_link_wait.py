"""`lium topup link`, `lium topup wait`, `--wait` on `topup create` / `topup card`, and `lium signup --billing-key`.

An agent with no card and no browser gets a payment page for a person (Stripe Checkout), or a crypto invoice,
and then needs to know when the money is on the balance: the webhook credits it after the command that
started the payment has returned. `--wait` polls the balance and reports `seconds_to_credit`.
"""

import json

import pytest
import responses
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.signup import actions as signup_actions
from lium.cli.topup import command as topup_module
from lium.sdk import Lium, LiumPermissionError
from lium.sdk.config import Config

API = "https://lium.io/api"
CHECKOUT_URL = "https://checkout.stripe.com/c/pay/cs_test_a1"


@pytest.fixture
def client():
    return Lium(Config(api_key="test-key"))


# -- SDK: topup_checkout_link


@responses.activate
def test_checkout_link_posts_the_amount_and_urls_and_returns_the_page(client):
    responses.post(f"{API}/stripe/create-checkout-session",
                   json={"id": "cs_test_a1", "url": CHECKOUT_URL, "expires_at": 1790500000})

    link = client.topup_checkout_link(10)

    assert link == {"url": CHECKOUT_URL, "session_id": "cs_test_a1", "amount_usd": 10, "expires_at": 1790500000}
    body = json.loads(responses.calls[0].request.body)
    assert body == {"amount": 10, "success_url": "https://lium.io/billing?success=true",
                    "cancel_url": "https://lium.io/billing", "mode": "payment"}


@responses.activate
def test_checkout_link_without_a_url_is_an_error_not_an_empty_link(client):
    responses.post(f"{API}/stripe/create-checkout-session", json={"id": "cs_test_a1"})

    with pytest.raises(Exception, match="without a payment page URL"):
        client.topup_checkout_link(10)


@responses.activate
def test_checkout_link_with_a_key_lacking_billing_is_a_permission_error(client):
    responses.post(f"{API}/stripe/create-checkout-session", status=403,
                   json={"success": False, "error": {"code": "forbidden",
                                                     "message": "API key 'Default' does not have the 'billing' scope"}})

    with pytest.raises(LiumPermissionError):
        client.topup_checkout_link(10)


def test_checkout_link_refuses_a_nan_amount_before_any_request(client):
    with pytest.raises(Exception, match="finite"):
        client.topup_checkout_link(float("nan"))


# -- SDK: wait_for_credit


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def test_wait_for_credit_returns_once_the_balance_rises(client, monkeypatch):
    readings = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr(client, "balance", lambda: next(readings))
    clock = _Clock()

    outcome = client.wait_for_credit(0.0, timeout=60, interval=2, _clock=clock, _sleep=clock.sleep)

    assert outcome == {"credited": True, "balance": 10.0, "seconds": 4.0}


def test_wait_for_credit_gives_up_at_the_timeout_with_the_last_balance(client, monkeypatch):
    monkeypatch.setattr(client, "balance", lambda: 3.0)
    clock = _Clock()

    outcome = client.wait_for_credit(3.0, timeout=5, interval=2, _clock=clock, _sleep=clock.sleep)

    assert outcome["credited"] is False
    assert outcome["balance"] == 3.0
    assert outcome["seconds"] <= 5


def test_wait_for_credit_rides_over_a_failed_balance_read(client, monkeypatch):
    import requests

    readings = iter([requests.ConnectionError("reset"), 12.5])

    def balance():
        value = next(readings)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(client, "balance", balance)
    clock = _Clock()

    outcome = client.wait_for_credit(2.5, timeout=60, interval=2, _clock=clock, _sleep=clock.sleep)

    assert outcome["credited"] is True and outcome["balance"] == 12.5


def test_wait_for_credit_ignores_float_noise(client, monkeypatch):
    monkeypatch.setattr(client, "balance", lambda: 5.000001)
    clock = _Clock()

    assert client.wait_for_credit(5.0, timeout=3, interval=2, _clock=clock, _sleep=clock.sleep)["credited"] is False


# -- CLI


class _FakeLium:
    """One account: `balances` is what successive balance reads return (the last one repeats)."""

    made_with: list = []
    balances: list = [0.0]
    link: dict = {"url": CHECKOUT_URL, "session_id": "cs_test_a1", "amount_usd": 10, "expires_at": None}
    charged: list = []

    def __init__(self, config=None):
        type(self).made_with.append(config.api_key if config is not None else None)

    def balance(self):
        values = type(self).balances
        value = values.pop(0) if len(values) > 1 else values[0]
        if isinstance(value, Exception):
            raise value
        return value

    def wait_for_credit(self, baseline, timeout=600, interval=2.0):
        seen = self.balance()
        if seen > baseline:
            return {"credited": True, "balance": seen, "seconds": 3.0}
        return {"credited": False, "balance": seen, "seconds": timeout}

    def topup_checkout_link(self, amount):
        return {**type(self).link, "amount_usd": amount}

    def topup_create_invoice(self, amount, crypto_currency, crypto_network):
        return {"invoice_id": "inv-1", "deposit_address": "0xabc", "crypto_amount": amount,
                "crypto_currency": crypto_currency, "crypto_network": crypto_network}

    def topup_card(self, amount, payment_method_id=None, idempotency_key=None):
        type(self).charged.append(amount)
        return {"status": "succeeded", "payment_intent_id": "pi_1", "idempotency_key": "k-1", "amount_usd": amount}


@pytest.fixture
def fake_lium(monkeypatch):
    fake = type("FakeLium", (_FakeLium,), {"made_with": [], "balances": [0.0], "charged": []})
    monkeypatch.setattr(topup_module, "Lium", fake)
    monkeypatch.delenv("LIUM_BILLING_API_KEY", raising=False)
    monkeypatch.setattr(topup_module.config, "get", lambda key, default=None: None)
    return fake


def _envelope(result):
    return json.loads(result.stderr.strip().splitlines()[-1])


def test_link_prints_the_page_the_baseline_and_the_handoff(fake_lium):
    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["url"] == CHECKOUT_URL
    assert out["balance_before"] == 0.0
    assert out["handoff"]["step"] == "card_payment"
    assert "credited" not in out


def test_link_uses_the_billing_key_from_the_environment(fake_lium, monkeypatch):
    monkeypatch.setenv("LIUM_BILLING_API_KEY", "sk_billing_only")

    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", "--json"])

    assert result.exit_code == 0, result.output
    assert "sk_billing_only" in fake_lium.made_with


def test_link_uses_the_billing_key_saved_by_signup(fake_lium, monkeypatch):
    monkeypatch.setattr(topup_module.config, "get",
                        lambda key, default=None: "sk_saved_billing" if key == "api.billing_api_key" else None)

    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", "--json"])

    assert result.exit_code == 0, result.output
    assert "sk_saved_billing" in fake_lium.made_with


def test_link_wait_announces_the_page_on_stderr_then_reports_the_credit(fake_lium):
    fake_lium.balances = [0.0, 10.0]

    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", "--wait", "60", "--json"])

    assert result.exit_code == 0, result.output
    handoff = json.loads(result.stderr.strip().splitlines()[0])
    assert handoff["event"] == "handoff" and handoff["url"] == CHECKOUT_URL
    out = json.loads(result.stdout)
    assert out["credited"] is True
    assert out["balance"] == 10.0
    assert out["seconds_to_credit"] == 3.0
    assert out["url"] == CHECKOUT_URL


def test_link_wait_timeout_is_credit_not_seen_exit_3_with_the_session(fake_lium):
    fake_lium.balances = [0.0]

    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", "--wait", "5", "--json"])

    assert result.exit_code == 3
    assert result.stdout == ""
    envelope = _envelope(result)
    assert envelope["error"]["code"] == "credit_not_seen"
    assert envelope["data"]["session_id"] == "cs_test_a1"
    assert envelope["data"]["balance_before"] == 0.0


@pytest.mark.parametrize("args", [["--wait", "0"], ["--wait", "-3"], ["--wait", "nan"]])
def test_a_wait_that_is_not_a_positive_number_is_refused(fake_lium, args):
    result = CliRunner().invoke(cli, ["topup", "link", "-a", "10", *args, "--json"])

    assert result.exit_code == 2
    assert _envelope(result)["error"]["code"] == "invalid_arguments"


def test_create_wait_reports_the_invoice_first_then_the_credit(fake_lium):
    fake_lium.balances = [1.0, 21.0]

    result = CliRunner().invoke(cli, ["topup", "create", "-a", "20", "-c", "USDC", "-n", "base", "--wait", "900", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stderr.strip().splitlines()[0])["deposit_address"] == "0xabc"
    out = json.loads(result.stdout)
    assert out["invoice_id"] == "inv-1"
    assert out["balance_before"] == 1.0 and out["seconds_to_credit"] == 3.0


def test_create_without_wait_prints_only_the_invoice(fake_lium):
    result = CliRunner().invoke(cli, ["topup", "create", "-a", "20", "-c", "USDC", "-n", "base", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["deposit_address"] == "0xabc"
    assert result.stderr == ""


def test_card_wait_reads_the_baseline_before_charging_and_reports_the_credit(fake_lium):
    fake_lium.balances = [4.0, 54.0]

    result = CliRunner().invoke(cli, ["topup", "card", "-a", "50", "--yes", "--wait", "60", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["balance_before"] == 4.0
    assert out["balance"] == 54.0
    assert out["seconds_to_credit"] == 3.0
    assert fake_lium.charged == [50.0]


def test_card_wait_with_an_unreadable_balance_charges_nothing(fake_lium):
    fake_lium.balances = [RuntimeError("down")]

    result = CliRunner().invoke(cli, ["topup", "card", "-a", "50", "--yes", "--wait", "60", "--json"])

    assert result.exit_code == 3
    assert _envelope(result)["error"]["code"] == "balance_unreadable"
    assert fake_lium.charged == []


def test_card_wait_timeout_keeps_the_idempotency_key_so_a_retry_does_not_charge_twice(fake_lium):
    fake_lium.balances = [4.0]

    result = CliRunner().invoke(cli, ["topup", "card", "-a", "50", "--yes", "--wait", "5", "--json"])

    assert result.exit_code == 3
    envelope = _envelope(result)
    assert envelope["error"]["code"] == "credit_not_seen"
    assert envelope["data"]["idempotency_key"] == "k-1"
    assert fake_lium.charged == [50.0]


def test_wait_command_waits_above_the_given_balance(fake_lium):
    fake_lium.balances = [10.0]

    result = CliRunner().invoke(cli, ["topup", "wait", "--above", "0", "--timeout", "30", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"credited": True, "balance_before": 0.0, "balance": 10.0,
                                         "seconds_to_credit": 3.0}


# -- signup --billing-key


class _Response:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def signup_env(monkeypatch):
    stored = {}

    class FakeConfig:
        def get(self, key, default=None):
            return stored.get(key, default)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(signup_actions, "config", FakeConfig())
    monkeypatch.setattr("lium.cli.init.actions.SetupSshKeyAction.execute",
                        lambda self, ctx: ActionResult(ok=True, data={}))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    return stored


def _fake_server(monkeypatch, key_response):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/users"):
            return _Response(200, {"api_key": "sk_rent_key", "signup_credit_granted": False})
        if url.endswith("/users/login"):
            return _Response(200, {"token": "jwt-1"})
        if url.endswith("/keys"):
            return key_response
        raise AssertionError(url)

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    return calls


def test_signup_billing_key_mints_a_billing_only_key_with_the_session_and_stores_it(monkeypatch, signup_env):
    calls = _fake_server(monkeypatch, _Response(200, {"id": "k2", "key": "sk_billing", "scopes": ["billing"]}))

    result = CliRunner().invoke(cli, ["signup", "--email", "a@example.com", "--billing-key", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["api_key"] == "sk_rent_key"
    assert out["billing_api_key"] == "sk_billing" and out["billing_key_configured"] is True
    assert signup_env == {"api.api_key": "sk_rent_key", "api.billing_api_key": "sk_billing"}
    keys_call = [kw for url, kw in calls if url.endswith("/keys")][0]
    assert keys_call["json"] == {"name": "agent-billing", "scopes": ["billing"]}
    assert keys_call["headers"] == {"Authorization": "Bearer jwt-1"}


def test_signup_without_the_flag_mints_no_billing_key(monkeypatch, signup_env):
    calls = _fake_server(monkeypatch, _Response(500))

    result = CliRunner().invoke(cli, ["signup", "--email", "a@example.com", "--json"])

    assert result.exit_code == 0, result.output
    assert "billing_api_key" not in json.loads(result.stdout)
    assert not [url for url, _ in calls if url.endswith("/keys")]


def test_a_refused_billing_key_is_reported_and_the_account_stands(monkeypatch, signup_env):
    _fake_server(monkeypatch, _Response(422, {"detail": "unknown scope"}))

    result = CliRunner().invoke(cli, ["signup", "--email", "a@example.com", "--billing-key", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["billing_key_configured"] is False
    assert "unknown scope" in out["billing_key_error"]
    assert "api.billing_api_key" not in signup_env
    assert signup_env["api.api_key"] == "sk_rent_key"


def test_a_key_minted_with_wider_scopes_is_never_kept_as_the_money_key(monkeypatch, signup_env):
    _fake_server(monkeypatch, _Response(200, {"id": "k2", "key": "sk_wide", "scopes": ["read", "rent", "billing"]}))

    result = CliRunner().invoke(cli, ["signup", "--email", "a@example.com", "--billing-key", "--json"])

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["billing_key_configured"] is False
    assert "api.billing_api_key" not in signup_env
