import json
import sys
import types

from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli import balance as balance_module
from lium.cli.cli import cli
from lium.cli.fund import command as fund_module
from lium.sdk import Config, Lium, NowPaymentsCurrency, NowPaymentsInvoice


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_sdk_lists_nowpayments_currencies(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return _Response(
            {
                "currencies": [
                    {
                        "code": "usdttrc20",
                        "name": "Tether USD",
                        "logo_url": "https://example.com/usdt.png",
                        "network": "tron",
                    }
                ]
            }
        )

    monkeypatch.setattr(client, "_request", fake_request)

    currencies = client.nowpayments_currencies(refresh=True)

    assert currencies == [
        NowPaymentsCurrency(
            code="usdttrc20",
            name="Tether USD",
            logo_url="https://example.com/usdt.png",
            network="tron",
        )
    ]
    assert calls == [
        (
            "GET",
            "/nowpayments/currencies",
            {"params": {"refresh": True}},
        )
    ]


def test_sdk_creates_nowpayments_invoice(monkeypatch):
    client = Lium(Config(api_key="test-key"))
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint, kwargs))
        return _Response(
            {
                "invoice_url": "https://nowpayments.example/invoice",
                "invoice_id": "inv_123",
                "transaction_id": "txn_123",
                "target_address": "wallet-address",
                "target_currency": "usdttrc20",
                "target_amount": 24.9,
                "payment_id": "pay_123",
                "payment_status": "waiting",
                "payin_extra_id": "memo-1",
                "network": "tron",
                "expires_at": "2026-04-29T18:00:00Z",
            }
        )

    monkeypatch.setattr(client, "_request", fake_request)

    invoice = client.create_nowpayments_invoice(amount_usd=25, pay_currency="usdttrc20")

    assert invoice == NowPaymentsInvoice(
        invoice_url="https://nowpayments.example/invoice",
        invoice_id="inv_123",
        transaction_id="txn_123",
        target_address="wallet-address",
        target_currency="usdttrc20",
        target_amount=24.9,
        payment_id="pay_123",
        payment_status="waiting",
        payin_extra_id="memo-1",
        network="tron",
        expires_at="2026-04-29T18:00:00Z",
    )
    assert calls == [
        (
            "POST",
            "/nowpayments/create-invoice",
            {"json": {"amount": 25, "pay_currency": "usdttrc20"}},
        )
    ]


def test_fund_help_documents_crypto_agent_flow():
    result = CliRunner().invoke(cli, ["fund", "--help"])

    assert result.exit_code == 0
    assert "legacy TAO funding flow" in result.output
    assert "lium fund crypto currencies --json" in result.output
    assert (
        "lium fund crypto invoice --amount-usd 25 --currency usdttrc20 --json"
        in result.output
    )
    assert "never read private keys" in result.output


def test_crypto_currencies_json(monkeypatch):
    class FakeLium:
        def nowpayments_currencies(self, *, refresh=False):
            assert refresh is True
            return [
                NowPaymentsCurrency(
                    code="usdttrc20",
                    name="Tether USD",
                    logo_url="",
                    network="tron",
                )
            ]

    monkeypatch.setattr(fund_module, "Lium", FakeLium)

    result = CliRunner().invoke(
        cli, ["fund", "crypto", "currencies", "--refresh", "--json"]
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "currencies": [
            {
                "code": "usdttrc20",
                "name": "Tether USD",
                "logo_url": "",
                "network": "tron",
            }
        ]
    }


def test_crypto_invoice_json(monkeypatch):
    class FakeLium:
        def create_nowpayments_invoice(self, *, amount_usd, pay_currency):
            assert amount_usd == 25.0
            assert pay_currency == "usdttrc20"
            return NowPaymentsInvoice(
                invoice_url="https://nowpayments.example/invoice",
                invoice_id="inv_123",
                transaction_id="txn_123",
                target_address="wallet-address",
                target_currency="usdttrc20",
                target_amount=24.9,
                payment_id="pay_123",
                payment_status="waiting",
                payin_extra_id=None,
                network="tron",
                expires_at=None,
            )

    monkeypatch.setattr(fund_module, "Lium", FakeLium)

    result = CliRunner().invoke(
        cli,
        [
            "fund",
            "crypto",
            "invoice",
            "--amount-usd",
            "25",
            "--currency",
            "USDTTRC20",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.output) == {
        "amount_usd": 25.0,
        "pay_currency": "usdttrc20",
        "invoice_url": "https://nowpayments.example/invoice",
        "invoice_id": "inv_123",
        "transaction_id": "txn_123",
        "target_address": "wallet-address",
        "target_currency": "usdttrc20",
        "target_amount": 24.9,
        "payment_id": "pay_123",
        "payment_status": "waiting",
        "payin_extra_id": None,
        "network": "tron",
        "expires_at": None,
    }


def test_balance_json(monkeypatch):
    class FakeLium:
        def balance(self):
            return 42.5

    monkeypatch.setattr(balance_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["balance", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"balance_usd": 42.5}


def test_legacy_fund_dispatch_still_runs(monkeypatch):
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace())

    class FakeLium:
        def balance(self):
            return 10.0

    class FakeLoadWalletAction:
        def execute(self, ctx):
            return ActionResult(
                ok=True,
                data={"wallet": object(), "address": "coldkey"},
            )

    class FakeCheckWalletRegistrationAction:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"registered": True})

    class FakeExecuteTransferAction:
        def execute(self, ctx):
            assert ctx["tao_amount"] == 1.5
            return ActionResult(ok=True, data={})

    monkeypatch.setattr(fund_module, "Lium", FakeLium)
    monkeypatch.setattr(fund_module, "LoadWalletAction", FakeLoadWalletAction)
    monkeypatch.setattr(
        fund_module, "CheckWalletRegistrationAction", FakeCheckWalletRegistrationAction
    )
    monkeypatch.setattr(fund_module, "ExecuteTransferAction", FakeExecuteTransferAction)

    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5", "-y"])

    assert result.exit_code == 0
    assert "Done." in result.output
