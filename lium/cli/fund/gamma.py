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
    """Deposits have no spender: the runtime's ``mint_gamma`` takes no ``spender`` (bittensor <= 10
    reads the connected chain's call metadata; bittensor 11's generated builders follow the runtime
    it was built for)."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        call = substrate.get_metadata_call_function("SubtensorModule", "mint_gamma")
        return "spender" not in {field["name"] for field in getattr(call, "value", call)["fields"]}
    return "spender" not in inspect.signature(bt.calls.SubtensorModule.mint_gamma).parameters


def _held(subtensor, hotkey: str) -> int:
    """Gamma units already in the deposit at ``hotkey`` (minted before runtime 493 and not yet
    spent, or an earlier mint's slippage margin), so this payment spends them too."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        result = substrate.query("SubtensorModule", "GammaDeposits", [NETUID, hotkey])
    else:  # bittensor 11 reads storage by a (pallet, item) pair
        result = subtensor.query(("SubtensorModule", "GammaDeposits"), [NETUID, hotkey])
    deposit = getattr(result, "value", result)
    return int((deposit or {}).get("balance") or 0)


def _pending(bt, subtensor, address: str) -> bool:
    """A transaction from ``address`` is still in the pool: the pool's next nonce is past the chain's.
    Its outcome is unknown (a reply lost or interrupted), and the deposit read now may not show it yet."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        pool = substrate.get_account_next_index(address)
        account = substrate.query("System", "Account", [address])
    else:  # bittensor 11: prepare_call takes the pool's next nonce
        pool = subtensor.prepare_call(bt.calls.Utility.batch_all(calls=[]), address=address).nonce
        account = subtensor.query(("System", "Account"), [address])
    return int(pool) > int((getattr(account, "value", account) or {}).get("nonce") or 0)


def gamma_fund(bt, bt_wallet, coldkey_ss58: str, wallet: str, usd: float, yes: bool, json_output: bool) -> None:
    from .actions import UnlockColdkeyAction

    lium = Lium()  # before the unlock: a missing API key never costs a password prompt
    subtensor = validation.subtensor_class(bt)()
    gamma_per_tao = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, _UNITS))
    tao_rao = tao_rao_for_usd(usd, gamma_per_tao)
    expected = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, tao_rao))
    min_out = min_gamma_out(expected)
    tao = tao_rao / _UNITS
    # an earlier mint or spend whose outcome was unknown must settle first: until then the deposit read
    # below may still count gamma it spends, and a payment built on it would fail or be paid twice
    if ui.load("Checking for pending transactions", lambda: _pending(bt, subtensor, coldkey_ss58)):
        raise LiumError(
            f"An earlier transaction from {coldkey_ss58} is still pending. "
            "Run this again once it has landed (usually within a minute)."
        )

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

    def submit(calls: list[tuple[str, dict]]) -> tuple[bool, str | None]:
        """Sign ``calls`` (SubtensorModule functions) with the coldkey, as one ``batch_all`` when several."""
        substrate = getattr(subtensor, "substrate", None)
        if substrate is None:  # bittensor 11: raw calls go through submit_call
            built = [getattr(bt.calls.SubtensorModule, function)(**args) for function, args in calls]
            call = built[0] if len(built) == 1 else bt.calls.Utility.batch_all(calls=built)
            # the unlocked keypair, not the wallet: bittensor 11 unlocks a wallet again at signing,
            # and that second password prompt sits unseen under the spinner
            resp = subtensor.submit_call(call, coldkey, wait_for_inclusion=True, wait_for_finalization=True)
            return resp.success, None if resp.success else resp.message
        built = [
            substrate.compose_call(call_module="SubtensorModule", call_function=function, call_params=args)
            for function, args in calls
        ]
        call = built[0] if len(built) == 1 else substrate.compose_call(
            call_module="Utility", call_function="batch_all", call_params={"calls": built}
        )
        extrinsic = substrate.create_signed_extrinsic(call=call, keypair=coldkey)
        receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        return receipt.is_success, None if receipt.is_success else receipt.error_message

    def mint() -> tuple[bool, str | None]:
        return submit([("mint_gamma", params)] + ([("spend_gamma", spend)] if spec_493 else []))

    try:
        ok, error = ui.load("Minting gamma", mint)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - a submit that raised or was interrupted may still land
        ok, error = False, str(e) or type(e).__name__
    if not ok:
        raise CliFailure("transfer_failed", f"Gamma mint failed: {error}", EXIT_GENERAL_ERROR, hint=FAILED_HINT)

    # the batch spent the minimum it could count on: what the mint gave above it (the price margin) is
    # spent now, with the key already unlocked, so the whole payment is credited
    rest_spent = rest_pending = rest = 0
    if spec_493:
        try:
            rest = _held(subtensor, coldkey_ss58)
            if rest > 0:
                try:
                    rest_ok, rest_error = ui.load(
                        "Spending the rest at Lium",
                        lambda: submit([("spend_gamma", {"service": payee, "netuid": NETUID, "amount": rest})]),
                    )
                except (Exception, KeyboardInterrupt):  # sent, but the answer was lost
                    rest_pending = rest
                    raise
                if not rest_ok:  # the chain answered: refused, nothing spent
                    raise LiumError(rest_error or "spend failed")
                rest_spent = rest
        except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - the payment itself has landed
            if rest_pending:
                ui.warning(
                    f"Paid. Spending the {rest / _UNITS:.4f} gamma left in the deposit may still land ({e}); "
                    "Lium credits it if it does, and the next 'lium fund --gamma' waits until it has settled"
                )
            else:
                ui.warning(
                    f"Paid, but the gamma left in the deposit was not spent ({e}); "
                    "it is spent with your next 'lium fund --gamma'"
                )

    if json_output:
        click.echo(json.dumps({"ok": True, "tx": {
            "coldkey": coldkey_ss58, "netuid": NETUID, "usd": usd, "amount_tao": tao,
            "gamma_expected": expected / _UNITS, "gamma_min": min_out / _UNITS, "gamma_rest_spent": rest_spent / _UNITS,
            "gamma_rest_pending": rest_pending / _UNITS,
            "service" if spec_493 else "spender": payee,
        }}, sort_keys=True))
        return
    ui.success("Gamma minted. Lium credits it 1 gamma = 1 USD within a few minutes.")
    ui.info("Done.")
