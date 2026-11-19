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

import json
import time
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

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


ZERO_ACCOUNT = "5C4hrfjw9DjXZTzV3MwzrrAr9P1MJhSrvWGWqi1eSuyUpnhM"  # any well-formed key: the read is a probe


def _spec_493(subtensor) -> bool:
    """Deposits have no spender, read from the connected runtime, never the installed SDK: bittensor
    <= 10 reads ``mint_gamma``'s fields from the call metadata; bittensor 11, which exposes no call
    metadata, reads ``GammaSpent``, the storage runtime 493 adds with it (its codec raises
    ``StorageFunctionNotFound`` for an item the runtime lacks)."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        call = substrate.get_metadata_call_function("SubtensorModule", "mint_gamma")
        return "spender" not in {field["name"] for field in getattr(call, "value", call)["fields"]}
    try:
        subtensor.query(("SubtensorModule", "GammaSpent"), [NETUID, ZERO_ACCOUNT, ZERO_ACCOUNT])
    except Exception as e:  # only the runtime lacking the item means "before 493"
        if any(cls.__name__ == "StorageFunctionNotFound" for cls in type(e).__mro__):
            return False
        raise
    return True


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


# {coldkey: {"nonce": highest nonce it signed whose outcome is unknown, "at": unix time}}: kept across runs until
# finalized state resolves it, or until its mortal era has passed and it can no longer land
UNRESOLVED = Path.home() / ".lium" / "gamma_unresolved.json"
ERA_PERIOD = 128  # blocks a signed payment stays valid (bittensor 11's default; ~26 min at 12 s)
UNRESOLVED_EXPIRES = ERA_PERIOD * 12 + 600  # seconds, with a margin


class OutcomeUnknown(Exception):
    """Broadcast, but the answer was lost: the transaction may still land."""


def _unresolved() -> dict:
    try:
        return json.loads(UNRESOLVED.read_text())
    except (OSError, ValueError):
        return {}


def _write_unresolved(entries: dict) -> None:
    UNRESOLVED.parent.mkdir(parents=True, exist_ok=True)
    UNRESOLVED.write_text(json.dumps(entries))


def _remember_unresolved(address: str, nonce: int) -> None:
    entries = _unresolved()
    entries[address] = {"nonce": max(nonce, entries.get(address, {}).get("nonce", -1)), "at": time.time()}
    _write_unresolved(entries)


def _settled_nonce(subtensor, address: str) -> int:
    """The nonce this run signs with: the finalized chain's, once nothing from ``address`` is unsettled.

    Refuses while the pool holds a transaction from it, or while a nonce it signed with an unknown
    outcome (this or an earlier run) is not yet below the finalized nonce. Signing every call with the
    nonce checked here also closes the race of an earlier broadcast reaching the pool after the check:
    both carry this nonce, so at most one of them executes."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        pool = substrate.get_account_next_index(address)
        final = substrate.query("System", "Account", [address], block_hash=substrate.get_chain_finalised_head())
    else:  # bittensor 11: prepare_call takes the pool's next nonce
        pool = subtensor.prepare_call(("Utility", "batch_all", {"calls": []}), address=address).nonce
        final = subtensor.query(("System", "Account"), [address], block=subtensor.finalized_block)
    final = int((getattr(final, "value", final) or {}).get("nonce") or 0)
    recorded = _unresolved().get(address)
    if recorded is not None and time.time() - recorded["at"] > UNRESOLVED_EXPIRES:
        recorded = None  # its era has passed: it landed (final counts it) or can never land
    if int(pool) > final or (recorded is not None and final <= recorded["nonce"]):
        raise LiumError(
            f"An earlier transaction from {address} has not settled yet (its outcome was unknown or it is "
            "still in the pool). Run this again once it is finalized, usually within a minute."
        )
    if address in _unresolved():
        entries = _unresolved()
        entries.pop(address, None)
        _write_unresolved(entries)
    return final


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
    nonce = ui.load("Checking for unsettled transactions", lambda: _settled_nonce(subtensor, coldkey_ss58))

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
    spec_493 = _spec_493(subtensor)
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

    def submit(calls: list[tuple[str, dict]], nonce: int) -> tuple[bool, str | None]:
        """Sign ``calls`` (SubtensorModule functions) with the coldkey at ``nonce``, as one ``batch_all`` when
        several. False only when the chain answered; OutcomeUnknown when the answer was lost."""
        substrate = getattr(subtensor, "substrate", None)
        if substrate is None:  # bittensor 11: (module, function, params) calls, composed against the
            # connected runtime, not the SDK's generated builders (which follow the runtime it was built for);
            # calls nested in a batch are composed first, as bittensor 11 requires
            built = [("SubtensorModule", function, args) for function, args in calls]
            call = built[0] if len(built) == 1 else ("Utility", "batch_all", {"calls": [subtensor.compose(c) for c in built]})
            # signed here with the unlocked keypair at the checked nonce (submit_call takes no nonce, and given a
            # wallet bittensor 11 unlocks it again, its password prompt unseen under the spinner)
            unsigned = subtensor.prepare_call(call, address=coldkey_ss58, nonce=nonce, period=ERA_PERIOD)
            resp = subtensor.submit_signature(
                unsigned, coldkey.sign(unsigned.payload), wait_for_inclusion=True, wait_for_finalization=True
            )
            if not resp.success and (getattr(resp, "data", None) or {}).get("submission_outcome_unknown"):
                raise OutcomeUnknown(resp.message or "submission outcome unknown")
            return resp.success, None if resp.success else resp.message
        built = [
            substrate.compose_call(call_module="SubtensorModule", call_function=function, call_params=args)
            for function, args in calls
        ]
        call = built[0] if len(built) == 1 else substrate.compose_call(
            call_module="Utility", call_function="batch_all", call_params={"calls": built}
        )
        extrinsic = substrate.create_signed_extrinsic(
            call=call, keypair=coldkey, nonce=nonce, era={"period": ERA_PERIOD}  # mortal: see UNRESOLVED_EXPIRES
        )
        receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        return receipt.is_success, None if receipt.is_success else receipt.error_message

    def mint() -> tuple[bool, str | None]:
        return submit([("mint_gamma", params)] + ([("spend_gamma", spend)] if spec_493 else []), nonce)

    try:
        ok, error = ui.load("Minting gamma", mint)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - a submit that raised or was interrupted may still land
        _remember_unresolved(coldkey_ss58, nonce)  # the next run waits until finalized state resolves it
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
                        lambda: submit([("spend_gamma", {"service": payee, "netuid": NETUID, "amount": rest})], nonce + 1),
                    )
                except (Exception, KeyboardInterrupt):  # sent, but the answer was lost
                    rest_pending = rest
                    _remember_unresolved(coldkey_ss58, nonce + 1)
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
