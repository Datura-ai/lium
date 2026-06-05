"""Fund account command."""

import json
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import click
from rich.prompt import Prompt

from lium.sdk import Lium, LiumError
from lium.cli import ui
from lium.cli.utils import handle_errors, _emit_json_error
from lium.cli.settings import config
from . import validation
from .actions import (
    LoadWalletAction,
    CheckWalletRegistrationAction,
    ExecuteTransferAction,
    CheckFreeAlphaAction,
    ExecuteAlphaTransferAction,
)

# 1 alpha = 1e9 rao (same scale as TAO). Used to floor the API's Decimal alpha
# quote at integer-rao precision before constructing the on-chain Balance.
_RAO_PER_ALPHA = Decimal(10) ** 9

# Shown wherever a USD amount is confirmed: the listener credits the actual alpha
# moved, re-valued at on-chain inclusion time, so the credited USD can differ from
# the quote.
_USD_CAVEAT = "credited USD valued at on-chain inclusion time; may differ from quote"


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


def _alpha_fund(
    wallet: Optional[str],
    amount: Optional[str],
    hotkey: Optional[str],
    yes: bool,
    json_output: bool,
) -> None:
    """Fund the Lium account with free alpha via ``transfer_stake``.

    The amount is denominated in USD: ``GET /balance/convert/alpha`` quotes both the
    alpha amount to move and the subnet ``netuid`` to move it on, and
    ``GET /wallet/company/`` supplies the destination coldkey. All three values are
    resolved from pay-tao-api-v2 ONCE at the top of the flow — never hardcoded — so a
    rotated netuid/address cannot silently send alpha into a black hole. Any API
    failure ``raise``s ``LiumError`` (rendered as the JSON envelope under ``--json``)
    before any on-chain call. Mirrors the TAO flow's wallet-load + registration.
    """
    try:
        import bittensor as bt
    except ImportError:
        raise LiumError("Bittensor library not installed (pip install bittensor)")

    # In non-interactive (--json) mode we never prompt, so every required arg must be
    # supplied up front. Validate presence here — hotkey, then wallet, then amount —
    # so the error deterministically names the first missing arg regardless of the
    # resolution order below.
    if json_output:
        if not hotkey:
            raise LiumError(
                "hotkey is required for --alpha: pass --hotkey (SS58 or wallet hotkey name)"
            )
        if not wallet:
            raise LiumError("wallet is required: pass --wallet")
        if not amount:
            raise LiumError("amount is required: pass --amount")

    # Resolve the wallet (coldkey).
    if not wallet:
        default_wallet = config.get("funding.default_wallet", "default")
        wallet = Prompt.ask("Bittensor wallet name", default=default_wallet).strip()

    # Resolve and validate the origin hotkey. Accept either an SS58 address or a
    # wallet hotkey NAME (resolved against --wallet), mirroring btcli's stake-move.
    # --wallet is already resolved above, which a name lookup requires.
    if not hotkey:
        hotkey = Prompt.ask(
            "Origin hotkey (SS58 or wallet hotkey name) the alpha is staked under"
        ).strip()
    hotkey, error = validation.resolve_hotkey(hotkey, wallet, bt)
    if error:
        raise LiumError(error)

    result = LoadWalletAction().execute({"bt": bt, "wallet_name": wallet})
    if not result.ok:
        raise LiumError(f"Failed to load wallet '{wallet}': {result.error}")
    bt_wallet = result.data["wallet"]
    coldkey_ss58 = result.data["address"]

    # Resolve and validate the USD amount (fail fast, before any network call).
    if not amount:
        amount = Prompt.ask("Enter USD amount to fund").strip()
    usd_amount, error = validation.validate_amount(amount)
    if error:
        raise LiumError(error)

    # Register the wallet so the backend can attribute the deposit (mirrors TAO flow).
    lium = Lium()
    reg_ctx = {
        "lium": lium,
        "wallet_address": coldkey_ss58,
        "bt_wallet": bt_wallet,
    }
    result = ui.load(
        "Checking wallet registration",
        lambda: CheckWalletRegistrationAction().execute(reg_ctx),
    )
    if not result.ok:
        raise LiumError(f"Failed to register wallet: {result.error}")
    # app_id comes from the registration's single /tao/create-transfer parse when a
    # new wallet was registered; otherwise discover it (still exactly one round-trip).
    app_id = result.data.get("app_id") or lium._discover_app_id(bt_wallet)

    # API-first resolution: destination coldkey first (404-fast, no chain), then the
    # USD->alpha quote (503-prone). Both hard-fail (LiumError) on any API error.
    funding_address = ui.load(
        "Resolving funding address", lambda: lium.company_wallet(app_id)
    )
    # The destination is now trusted at runtime (no longer a source-pinned constant),
    # so validate it before it can become a transfer target: it must be a well-formed
    # SS58 and must not be the user's own coldkey. Abort (no fallback) otherwise.
    funding_address, fa_err = validation.validate_ss58(funding_address, bt)
    if fa_err:
        raise LiumError(f"pay API returned an invalid funding address: {fa_err}")
    if funding_address == coldkey_ss58:
        raise LiumError(
            "pay API returned the caller's own coldkey as the funding address; aborting"
        )
    quote = ui.load("Quoting alpha", lambda: lium.convert_alpha(usd_amount))
    netuid = quote.netuid

    # Decimal -> Balance: floor the quoted alpha at integer rao with ROUND_DOWN BEFORE
    # set_unit, so we never transfer more than quoted (keeps the free+fee gate exact).
    # Never via from_tao(float), which would inject binary-float error on a money path.
    floored_rao = int(
        (quote.alpha_amount * _RAO_PER_ALPHA).quantize(Decimal(1), rounding=ROUND_DOWN)
    )
    amount_bal = bt.Balance.from_rao(floored_rao).set_unit(netuid)
    alpha_amount = floored_rao / float(_RAO_PER_ALPHA)

    # Phase A: display free alpha + fee before the confirm prompt.
    check_ctx = {
        "bt": bt,
        "coldkey_ss58": coldkey_ss58,
        "hotkey_ss58": hotkey,
        "amount_bal": amount_bal,
        "netuid": netuid,
        "dest_coldkey": funding_address,
    }
    result = ui.load(
        "Checking free alpha", lambda: CheckFreeAlphaAction().execute(check_ctx)
    )
    if not result.ok:
        raise LiumError(result.error)
    free = result.data["free"]
    fee = result.data["fee"]
    fee_modeled = result.data["fee_modeled"]

    if not json_output:
        ui.info(f"Destination (Lium coldkey): {funding_address}")
        ui.info(f"Free alpha (netuid {netuid}): {free}")
        if fee_modeled:
            ui.info(f"Movement fee: {fee}")
        ui.info(_USD_CAVEAT)

    # Confirm. --json is treated as non-interactive: never prompt, never hang.
    if json_output and not yes:
        raise LiumError("confirmation required: pass --yes")
    if not json_output and not yes:
        if not ui.confirm(
            f"Fund account with ~${usd_amount} USD = {alpha_amount} alpha "
            f"(netuid {netuid}) from {hotkey} -> {funding_address}?",
            default=False,
        ):
            return

    # Phase B drift guard: re-fetch the quote and re-validate its netuid against the
    # value resolved at the top (value-vs-value, no constant). The TRANSFERRED amount
    # stays the Phase-A confirmed amount_bal — the re-fetch only guards netuid drift,
    # it never re-derives a new (possibly larger) signed amount.
    fresh_quote = lium.convert_alpha(usd_amount)
    if fresh_quote.netuid != netuid:
        raise LiumError(
            f"netuid changed mid-flight: resolved {netuid} but re-fetch returned "
            f"{fresh_quote.netuid}; aborting to avoid an uncredited transfer"
        )

    # Phase B: authoritative fee-aware gate (fresh re-read) + transfer_stake.
    exec_ctx = {
        "bt": bt,
        "bt_wallet": bt_wallet,
        "coldkey_ss58": coldkey_ss58,
        "hotkey_ss58": hotkey,
        "amount_bal": amount_bal,
        "netuid": netuid,
        "dest_coldkey": funding_address,
    }
    result = ExecuteAlphaTransferAction().execute(exec_ctx)
    if not result.ok:
        # A signed transfer the chain rejected is a hard failure: exit non-zero so
        # callers (and CI) detect it — unlike the pre-flight guard aborts above
        # (insufficient/no/ambiguous stake), which print and exit 0.
        if result.data.get("transfer_attempted"):
            if json_output:
                _emit_json_error("transfer_failed", result.error)  # exits non-zero
            raise click.ClickException(result.error)
        raise LiumError(result.error)

    fee_modeled = result.data.get("fee_modeled", fee_modeled)
    result_fee = result.data.get("fee")
    fee_caveat = (
        None
        if fee_modeled
        else "stake-movement fee not modeled; a max-amount send may fail to credit"
    )

    if json_output:
        envelope = {
            "ok": True,
            "tx": {
                "coldkey": coldkey_ss58,
                "hotkey": hotkey,
                "netuid": netuid,
                "usd": usd_amount,
                "amount_alpha": alpha_amount,
                "rate": float(quote.rate),
                "dest_coldkey": funding_address,
                "fee_alpha": float(result_fee.tao) if fee_modeled and result_fee is not None else None,
                "fee_modeled": fee_modeled,
                "fee_caveat": fee_caveat,
                "usd_caveat": _USD_CAVEAT,
            },
        }
        click.echo(json.dumps(envelope, sort_keys=True))
        return

    ui.success("Alpha transfer submitted.")
    ui.info("Done.")


@click.command("fund")
@click.option("--wallet", "-w", help="Bittensor wallet name to fund from")
@click.option("--amount", "-a", help="Amount to fund with (TAO; USD when --alpha)")
@click.option(
    "--alpha", is_flag=True, default=False, help="Fund with free Subnet-51 alpha stake"
)
@click.option(
    "--hotkey",
    "-k",
    default=None,
    help="Origin hotkey the alpha is staked under — SS58 address or wallet hotkey "
    "name (required with --alpha)",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False, help="Print machine-readable JSON"
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts")
@handle_errors
def fund_command(
    wallet: Optional[str],
    amount: Optional[str],
    alpha: bool,
    hotkey: Optional[str],
    json_output: bool,
    yes: bool,
):
    """Fund your Lium account with TAO (or Subnet-51 alpha) from a Bittensor wallet.

    \b
    Examples:
      lium fund
      lium fund -w default -a 1.5
      lium fund -w mywal -a 0.5 -y
      lium fund --alpha -k <hotkey-ss58> -a 25        # -a is USD when --alpha
      lium fund --alpha -w default -k myhotkey -a 25  # -k may be a wallet hotkey name
      lium fund --alpha -k <hotkey-ss58> -a 25 -y --json
    """
    if alpha:
        _alpha_fund(wallet, amount, hotkey, yes, json_output)
    else:
        _legacy_tao_fund(wallet, amount, yes)
