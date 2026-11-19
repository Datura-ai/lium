"""``lium fund --gamma``: pay USD with SN51 gamma minted from the wallet's TAO (DAH-6385).

``mint_gamma`` buys SN51 alpha with TAO, burns it and credits the gamma to the signer's
deposit at a hotkey; Lium spends deposits at hotkeys linked to the account (naming its
spender) and credits 1 gamma = 1 USD. The coldkey's own address serves as that hotkey,
as in the web Billing page, so one key signs both the link and the mint.

From subtensor runtime 493 deposits have no spender: the hotkey spends its own gamma with
``spend_gamma(service, 51, amount)`` at Lium's service, and Lium credits what it spent
there. The coldkey is that hotkey, so one ``batch_all`` mints and spends with one
signature, and a failed spend reverts the mint (DAH-6771).
"""

import inspect
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


def _sim_gamma(subtensor, rao: int) -> int:
    """Gamma units ``rao`` TAO mints right now (``GammaRuntimeApi.sim_mint_gamma``)."""
    params = [NETUID, rao]
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        raw = substrate.runtime_call("GammaRuntimeApi", "sim_mint_gamma", params)
    else:  # bittensor 11: no ``.substrate``; ``runtime`` returns the plain decoded value
        raw = subtensor.runtime(("GammaRuntimeApi", "sim_mint_gamma"), params)
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


def _spec_493(bt, subtensor) -> bool:
    """Deposits have no spender: the runtime has ``GammaSpent`` (bittensor <= 10 reads the chain's
    metadata; bittensor 11's generated calls follow the runtime it was built for)."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        return substrate.get_metadata_storage_function("SubtensorModule", "GammaSpent") is not None
    return "spender" not in inspect.signature(bt.calls.SubtensorModule.mint_gamma).parameters


def _held(subtensor, hotkey: str) -> int:
    """Gamma units already in the deposit at ``hotkey`` (minted before runtime 493 and not yet
    spent, or an earlier mint's slippage margin), so this payment spends them too."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        return 0  # shortcut: bittensor 11 reads are not wired here; the web Pay or btcli gamma spend covers a leftover
    deposit = getattr(substrate.query("SubtensorModule", "GammaDeposits", [NETUID, hotkey]), "value", None)
    return int((deposit or {}).get("balance") or 0)


def gamma_fund(bt, bt_wallet, coldkey_ss58: str, wallet: str, usd: float, yes: bool, json_output: bool) -> None:
    from .actions import UnlockColdkeyAction

    lium = Lium()  # before the unlock: a missing API key never costs a password prompt
    subtensor = validation.subtensor_class(bt)()
    gamma_per_tao = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, _UNITS))
    tao_rao = tao_rao_for_usd(usd, gamma_per_tao)
    expected = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, tao_rao))
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
    spec_493 = _spec_493(bt, subtensor)
    payee = link.get("service") if spec_493 else link.get("spender")
    if not payee:
        raise LiumError(
            f"Lium did not name a gamma {'service' if spec_493 else 'spender'}; "
            "the server may not support gamma top-ups yet"
        )

    params = {"hotkey": coldkey_ss58, "netuid": NETUID, "amount": tao_rao, "min_amount_out": min_out}
    if not spec_493:
        params["spender"] = payee
    params["daily_spend_cap"] = None
    spend = {"service": payee, "netuid": NETUID, "amount": min_out + _held(subtensor, coldkey_ss58)}

    def mint() -> tuple[bool, str | None]:
        substrate = getattr(subtensor, "substrate", None)
        if substrate is None:  # bittensor 11: raw calls go through submit_call
            call = bt.calls.SubtensorModule.mint_gamma(**params)
            if spec_493:
                call = bt.calls.Utility.batch_all(calls=[call, bt.calls.SubtensorModule.spend_gamma(**spend)])
            # the unlocked keypair, not the wallet: bittensor 11 unlocks a wallet again at signing,
            # and that second password prompt sits unseen under the spinner
            resp = subtensor.submit_call(call, coldkey, wait_for_inclusion=True, wait_for_finalization=True)
            return resp.success, None if resp.success else resp.message
        call = substrate.compose_call(call_module="SubtensorModule", call_function="mint_gamma", call_params=params)
        if spec_493:
            spend_call = substrate.compose_call(
                call_module="SubtensorModule", call_function="spend_gamma", call_params=spend
            )
            call = substrate.compose_call(
                call_module="Utility", call_function="batch_all", call_params={"calls": [call, spend_call]}
            )
        extrinsic = substrate.create_signed_extrinsic(call=call, keypair=coldkey)
        receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        return receipt.is_success, None if receipt.is_success else receipt.error_message

    try:
        ok, error = ui.load("Minting gamma", mint)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - a submit that raised or was interrupted may still land
        ok, error = False, str(e) or type(e).__name__
    if not ok:
        raise CliFailure("transfer_failed", f"Gamma mint failed: {error}", EXIT_GENERAL_ERROR, hint=FAILED_HINT)

    if json_output:
        click.echo(json.dumps({"ok": True, "tx": {
            "coldkey": coldkey_ss58, "netuid": NETUID, "usd": usd, "amount_tao": tao,
            "gamma_expected": expected / _UNITS, "gamma_min": min_out / _UNITS,
            "service" if spec_493 else "spender": payee,
        }}, sort_keys=True))
        return
    ui.success("Gamma minted. Lium credits it 1 gamma = 1 USD within a few minutes.")
    ui.info("Done.")
