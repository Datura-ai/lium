"""DAH-2154: NowPayments crypto funding is retired; TAO funding is kept.

The `lium fund` command continues to support the on-chain TAO wallet transfer
flow. The `lium fund crypto` NowPayments subgroup and its SDK helpers have been
removed.
"""

import json
import sys
import types

from click.testing import CliRunner

import lium.sdk as sdk
from lium.cli.actions import ActionResult
from lium.cli import balance as balance_module
from lium.cli.cli import cli
from lium.cli.fund import command as fund_module
from lium.sdk import Lium


def test_fund_help_documents_tao_flow():
    result = CliRunner().invoke(cli, ["fund", "--help"])

    assert result.exit_code == 0
    assert "TAO" in result.output
    assert "lium fund -w default -a 1.5" in result.output
    # The retired NowPayments crypto flow must not be advertised anymore.
    assert "crypto" not in result.output.lower()


def test_fund_tao_dispatch_runs(monkeypatch):
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


def test_fund_crypto_subcommand_is_retired():
    # The old NowPayments flow was `lium fund crypto invoice ...`; it must no
    # longer be a recognised command/option.
    result = CliRunner().invoke(
        cli,
        ["fund", "crypto", "invoice", "--amount-usd", "25", "--currency", "usdttrc20"],
    )

    assert result.exit_code != 0


def test_balance_json(monkeypatch):
    class FakeLium:
        def balance(self):
            return 42.5

    monkeypatch.setattr(balance_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["balance", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"balance_usd": 42.5}


def test_sdk_no_longer_exposes_nowpayments():
    assert not hasattr(sdk, "NowPaymentsCurrency")
    assert not hasattr(sdk, "NowPaymentsInvoice")
    assert not hasattr(Lium, "nowpayments_currencies")
    assert not hasattr(Lium, "create_nowpayments_invoice")
