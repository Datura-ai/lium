"""DAH-2154: CLI account funding (NowPayments + legacy TAO transfer) is retired.

The `lium fund` command now only emits a deprecation notice, and the SDK no
longer exposes the NowPayments helpers.
"""

import lium.sdk as sdk
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.sdk import Lium


def test_fund_without_subcommand_is_retired():
    result = CliRunner().invoke(cli, ["fund"])

    assert result.exit_code == 1
    assert "retired" in result.output.lower()


def test_fund_legacy_tao_invocation_is_retired():
    # The old on-chain TAO flow accepted -w/-a/-y; it must degrade to the notice
    # instead of attempting a transfer or erroring on the options.
    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5", "-y"])

    assert result.exit_code == 1
    assert "retired" in result.output.lower()


def test_fund_legacy_crypto_invocation_is_retired():
    # The old NowPayments flow was `lium fund crypto invoice ...`.
    result = CliRunner().invoke(
        cli,
        ["fund", "crypto", "invoice", "--amount-usd", "25", "--currency", "usdttrc20"],
    )

    assert result.exit_code == 1
    assert "retired" in result.output.lower()


def test_sdk_no_longer_exposes_nowpayments():
    assert not hasattr(sdk, "NowPaymentsCurrency")
    assert not hasattr(sdk, "NowPaymentsInvoice")
    assert not hasattr(Lium, "nowpayments_currencies")
    assert not hasattr(Lium, "create_nowpayments_invoice")
