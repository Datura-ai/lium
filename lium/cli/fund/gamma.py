"""``lium fund --gamma``: pay USD with SN51 gamma minted from the wallet's TAO (DAH-6385).

``mint_gamma`` buys SN51 alpha with TAO, burns it and credits the gamma to the signer's
deposit at a hotkey; Lium spends deposits at hotkeys linked to the account (naming its
spender) and credits 1 gamma = 1 USD. The coldkey's own address serves as that hotkey,
as in the web Billing page, so one key signs both the link and the mint.
"""

import json
from decimal import ROUND_CEILING, Decimal

import click

from lium.cli import ui
from lium.cli.utils import EXIT_GENERAL_ERROR, CliFailure
from lium.sdk import Lium, LiumError

from . import validation

NETUID = 51
_UNITS = 10**9  # rao per TAO and units per gamma
SLIPPAGE_BPS = 200  # the mint fails, costing only the fee, if the price moves 2% first

FAILED_HINT = (
    "Do not re-send yet: the mint may have reached the chain. Check 'lium balance' "
    "(gamma credits can take a few minutes) before funding again"
)


def _sim_gamma(substrate, rao: int) -> int:
    """Gamma units ``rao`` TAO mints right now (``GammaRuntimeApi.sim_mint_gamma``)."""
    raw = substrate.runtime_call("GammaRuntimeApi", "sim_mint_gamma", [NETUID, rao])
    value = getattr(raw, "value", raw)
    ok = value.get("Ok") if isinstance(value, dict) else None
    if not ok:
        raise LiumError(f"SN51 cannot quote a gamma mint right now: {value}")
    return int(ok[2])  # (alpha burned, TAO value, gamma)


def tao_rao_for_usd(usd: float, gamma_per_tao: int) -> int:
    """TAO (rao) that mints ``usd`` gamma at ``gamma_per_tao`` units per TAO, rounded up."""
    dollars = Decimal(str(usd)).quantize(Decimal("0.01"))
    return int((dollars * _UNITS * _UNITS / gamma_per_tao).to_integral_value(ROUND_CEILING))


def min_gamma_out(expected: int) -> int:
    return expected * (10_000 - SLIPPAGE_BPS) // 10_000


def gamma_fund(bt, bt_wallet, coldkey_ss58: str, wallet: str, usd: float, yes: bool, json_output: bool) -> None:
    from .actions import UnlockColdkeyAction

    lium = Lium()  # before the unlock: a missing API key never costs a password prompt
    substrate = validation.subtensor_class(bt)().substrate
    gamma_per_tao = ui.load("Quoting gamma", lambda: _sim_gamma(substrate, _UNITS))
    tao_rao = tao_rao_for_usd(usd, gamma_per_tao)
    expected = ui.load("Quoting gamma", lambda: _sim_gamma(substrate, tao_rao))
    min_out = min_gamma_out(expected)
    tao = tao_rao / _UNITS

    if json_output and not yes:
        raise LiumError("confirmation required: pass --yes")
    if not yes and not ui.confirm(
        f"Mint ~{expected / _UNITS:.2f} gamma (~${expected / _UNITS:.2f}) on SN51 with {tao:.6f} TAO "
        f"from {coldkey_ss58}? Fails if under {min_out / _UNITS:.2f} gamma at inclusion.",
        default=False,
    ):
        return

    result = UnlockColdkeyAction().execute({"bt_wallet": bt_wallet})
    if not result.ok:
        raise LiumError(f"Failed to unlock coldkey for wallet '{wallet}': {result.error}")
    coldkey = bt_wallet.coldkey

    link = ui.load(
        "Linking wallet to your Lium account",
        lambda: lium.link_gamma_hotkey(coldkey_ss58, lambda m: "0x" + coldkey.sign(m).hex()),
    )
    spender = link.get("spender")
    if not spender:
        raise LiumError("Lium did not name a gamma spender; the server may not support gamma top-ups yet")

    def mint():
        call = substrate.compose_call(
            call_module="SubtensorModule",
            call_function="mint_gamma",
            call_params={
                "hotkey": coldkey_ss58,
                "netuid": NETUID,
                "amount": tao_rao,
                "min_amount_out": min_out,
                "spender": spender,
                "daily_spend_cap": None,
            },
        )
        extrinsic = substrate.create_signed_extrinsic(call=call, keypair=coldkey)
        return substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)

    try:
        receipt = ui.load("Minting gamma", mint)
        ok, error = receipt.is_success, None if receipt.is_success else receipt.error_message
    except Exception as e:  # noqa: BLE001 - a submit that raised may still have landed
        ok, error = False, str(e)
    if not ok:
        raise CliFailure("transfer_failed", f"Gamma mint failed: {error}", EXIT_GENERAL_ERROR, hint=FAILED_HINT)

    if json_output:
        click.echo(json.dumps({"ok": True, "tx": {
            "coldkey": coldkey_ss58, "netuid": NETUID, "usd": usd, "amount_tao": tao,
            "gamma_expected": expected / _UNITS, "gamma_min": min_out / _UNITS, "spender": spender,
        }}, sort_keys=True))
        return
    ui.success("Gamma minted. Lium credits it 1 gamma = 1 USD within a few minutes.")
    ui.info("Done.")
