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

import ast
import json
import os
import tempfile
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
NOT_SENT_HINT = "Nothing was signed or sent, so nothing can land: fix the error above and run the command again"
REFUSED_HINT = "The node rejected this payment. Fix the error above and run the command again."


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


def _deposit(subtensor, hotkey: str) -> dict:
    """The deposit at ``hotkey`` as stored (``{}`` without one)."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        result = substrate.query("SubtensorModule", "GammaDeposits", [NETUID, hotkey])
    else:  # bittensor 11 reads storage by a (pallet, item) pair
        result = subtensor.query(("SubtensorModule", "GammaDeposits"), [NETUID, hotkey])
    return getattr(result, "value", result) or {}


def _held(subtensor, hotkey: str) -> int:
    """Gamma units already in the deposit at ``hotkey`` (minted before runtime 493 and not yet
    spent, or an earlier mint's slippage margin), so this payment spends them too."""
    return int(_deposit(subtensor, hotkey).get("balance") or 0)


CAP_REFILL_BLOCKS = 7200  # a daily spend cap's bucket refills its whole cap over a day of 12 s blocks


def _cap_allows(deposit: dict, block: int) -> tuple[int, int] | None:
    """(cap, units the deposit's daily spend cap lets it spend at ``block``), None without a cap.

    shortcut: replays the bucket's linear refill (cap per CAP_REFILL_BLOCKS) at the finalized block, which is at or
    below the block the payment lands in, so it never overstates; read the runtime's own figure once a runtime API
    returns the cap's allowance without the balance bound."""
    cap = deposit.get("daily_spend_cap")
    if not cap:
        return None
    limit = int(cap["cap"])
    tokens, last = cap["bucket"]
    return limit, min(limit, int(tokens) + limit * max(0, block - int(last)) // CAP_REFILL_BLOCKS)


# one file per coldkey, {"nonce": highest nonce it signed whose outcome is not known yet, "dies": finalized block after
# which none of those can land}: written before broadcasting and kept across runs until finalized state resolves it (its
# nonce, or finalized blocks passing its mortal era; never wall time, so a stalled chain keeps it unresolved). One file
# per coldkey, replaced atomically, needs no lock: concurrent runs for other wallets never share a file.
UNRESOLVED = Path.home() / ".lium" / "gamma_unresolved"
ERA_PERIOD = 128  # blocks a signed payment stays valid (bittensor 11's default; ~26 min at 12 s)


class OutcomeUnknown(Exception):
    """Broadcast, but the answer was lost: the transaction may still land."""


class NotSent(Exception):
    """Failed before anything was signed and broadcast (composing or signing): nothing can land."""


class Refused(NotSent):
    """The node refused the transaction on its only send: it never entered the pool, so it cannot land."""


def _recorded(address: str) -> dict | None:
    """The coldkey's unresolved payment, or None. An unreadable record refuses: it may hide a payment that can land."""
    path = UNRESOLVED / f"{address}.json"
    try:
        entry = json.loads(path.read_text())
        return {"nonce": int(entry["nonce"]), "dies": int(entry["dies"])}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise LiumError(
            f"Cannot read {path} ({e}), the record of a gamma payment from {address} whose outcome is unknown. "
            "Check the wallet's recent transactions, then delete the file to pay again."
        ) from e


def _record(address: str, nonce: int, dies: int) -> dict:
    known = _recorded(address) or {}
    entry = {"nonce": max(nonce, known.get("nonce", -1)), "dies": max(dies, known.get("dies", -1))}
    UNRESOLVED.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=UNRESOLVED, prefix=f".{address}.")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(entry))
        f.flush()
        os.fsync(f.fileno())  # on disk before the broadcast it describes
    os.replace(tmp, UNRESOLVED / f"{address}.json")
    return entry


def _forget(address: str, entry: dict) -> None:
    """Drops the record if it is still ``entry`` (another run may have recorded a newer payment meanwhile)."""
    if _recorded(address) == entry:
        (UNRESOLVED / f"{address}.json").unlink(missing_ok=True)


def _node_refusal(error: Exception) -> str | None:
    """The node's definitive refusal of a transaction it was sent once, or None when the outcome is not proved.

    bittensor <= 10's substrate library raises the node's JSON-RPC answer to author_submitAndWatchExtrinsic as the
    ``str()`` of the whole answer (``"{'jsonrpc': '2.0', 'id': 1, 'error': {'code': 1010, ...}}"``); the answer as a
    dict and the error object alone are read too. Only 1010 (invalid, e.g. fees it cannot pay) proves the transaction
    never entered the pool, and only on the first send (the caller sends once). 1013 (already imported), -32603 and
    any other answer leave the outcome unknown, as bittensor 11 treats them.
    """
    answer = error.args[0] if error.args else None
    if isinstance(answer, str):
        try:
            answer = ast.literal_eval(answer)  # the str() of a dict; literal_eval only reads literals
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
    if isinstance(answer, dict) and isinstance(answer.get("error"), dict):
        answer = answer["error"]
    if not isinstance(answer, dict) or answer.get("code") != 1010:
        return None
    return f"{answer['code']} {answer.get('message', '')}: {answer.get('data', '')}".rstrip(": ")


def _finalized(subtensor) -> tuple[object, int]:
    """The finalized head (as the stack's reads take it) and its block number."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        head = substrate.get_chain_finalised_head()
        return head, int(substrate.get_block_number(head))
    number = int(subtensor.finalized_block)
    return number, number


def _settled_nonce(subtensor, address: str) -> int:
    """The nonce this run signs with: the finalized chain's, once nothing from ``address`` is unsettled.

    Refuses while the pool holds a transaction from it, or while a nonce it signed with an unknown
    outcome (this or an earlier run) is not yet below the finalized nonce. Signing every call with the
    nonce checked here also closes the race of an earlier broadcast reaching the pool after the check:
    both carry this nonce, so at most one of them executes."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        pool = substrate.get_account_next_index(address)
    else:  # bittensor 11: prepare_call takes the pool's next nonce
        pool = subtensor.prepare_call(("Utility", "batch_all", {"calls": []}), address=address).nonce
    head, number = _finalized(subtensor)
    if substrate is not None:
        final = substrate.query("System", "Account", [address], block_hash=head)
    else:
        final = subtensor.query(("System", "Account"), [address], block=head)
    final = int((getattr(final, "value", final) or {}).get("nonce") or 0)
    recorded = _recorded(address)
    # settled once finalized state counts its nonce, or finalized blocks are past its mortal era: it can never land
    unsettled = recorded is not None and final <= recorded["nonce"] and number <= recorded["dies"]
    if int(pool) > final or unsettled:
        raise LiumError(
            f"An earlier transaction from {address} has not settled yet (its outcome was unknown or it is "
            "still in the pool). Run this again once it is finalized, usually within a minute."
        )
    if recorded is not None:
        _forget(address, recorded)
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
    spec_493 = _spec_493(subtensor)
    deposit = _deposit(subtensor, coldkey_ss58)
    held = int(deposit.get("balance") or 0)
    if spec_493:
        # the batch spends the mint's minimum plus what the deposit holds; a daily spend cap below that refuses the
        # spend and batch_all reverts the mint (the fee is lost), so it is refused here, before anything is signed
        need = min_out + held
        capped = _cap_allows(deposit, _finalized(subtensor)[1])
        if capped is not None and capped[1] < need:
            cap, allows = capped
            if need > cap:
                wait = "Waiting does not help: the payment is above the whole cap."
            else:
                hours = -(-(need - allows) * CAP_REFILL_BLOCKS // cap) * 12 / 3600
                wait = f"Or wait about {hours:.1f} h for the cap to refill to {need / _UNITS:.2f} gamma."
            raise CliFailure(
                "gamma_spend_cap",
                f"The gamma deposit at {coldkey_ss58} has a daily spend cap of {cap / _UNITS:.2f} gamma and can spend "
                f"{allows / _UNITS:.2f} gamma now; this payment spends {need / _UNITS:.2f} gamma (at least "
                f"{min_out / _UNITS:.2f} minted plus {held / _UNITS:.2f} already in the deposit), so the chain would "
                "refuse it.",
                EXIT_GENERAL_ERROR,
                hint=(
                    f"Raise or remove the cap with set_gamma_deposit_daily_spend_cap (SN51, hotkey {coldkey_ss58}, "
                    f"signed by this coldkey) to at least {need / _UNITS:.2f} gamma, then run this again. {wait} "
                    "Nothing was signed or sent."
                ),
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
    spend = {"service": payee, "netuid": NETUID, "amount": min_out + held}

    def submit(calls: list[tuple[str, dict]], nonce: int) -> tuple[bool, str | None]:
        """Sign ``calls`` (SubtensorModule functions) with the coldkey at ``nonce``, as one ``batch_all`` when
        several. False only when the chain answered; OutcomeUnknown when the answer was lost. Records the payment
        before broadcasting (so a crash or kill after it still leaves the record) and drops it once the chain answers."""
        substrate = getattr(subtensor, "substrate", None)
        if substrate is None:  # bittensor 11: (module, function, params) calls, composed against the
            # connected runtime, not the SDK's generated builders (which follow the runtime it was built for);
            # calls nested in a batch are composed first, as bittensor 11 requires
            built = [("SubtensorModule", function, args) for function, args in calls]
            # signed here with the unlocked keypair at the checked nonce (submit_call takes no nonce, and given a
            # wallet bittensor 11 unlocks it again, its password prompt unseen under the spinner)
            try:
                call = built[0] if len(built) == 1 else ("Utility", "batch_all", {"calls": [subtensor.compose(c) for c in built]})
                unsigned = subtensor.prepare_call(call, address=coldkey_ss58, nonce=nonce, period=ERA_PERIOD)
                signature = coldkey.sign(unsigned.payload)
                # bittensor 11 anchors the era to a finalized block, so at or below the one read after it
                entry = _record(coldkey_ss58, nonce, _finalized(subtensor)[1] + ERA_PERIOD)
            except (Exception, KeyboardInterrupt) as e:  # Ctrl-C included: nothing is broadcast yet
                raise NotSent(str(e) or type(e).__name__) from e
            resp = subtensor.submit_signature(unsigned, signature, wait_for_inclusion=True, wait_for_finalization=True)
            if not resp.success and (getattr(resp, "data", None) or {}).get("submission_outcome_unknown"):
                raise OutcomeUnknown(resp.message or "submission outcome unknown")
            _forget(coldkey_ss58, entry)
            return resp.success, None if resp.success else resp.message
        try:
            built = [
                substrate.compose_call(call_module="SubtensorModule", call_function=function, call_params=args)
                for function, args in calls
            ]
            call = built[0] if len(built) == 1 else substrate.compose_call(
                call_module="Utility", call_function="batch_all", call_params={"calls": built}
            )
            _, born = _finalized(subtensor)  # the era starts at this finalized block, not the best head
            extrinsic = substrate.create_signed_extrinsic(
                call=call, keypair=coldkey, nonce=nonce, era={"period": ERA_PERIOD, "current": born}
            )
            entry = _record(coldkey_ss58, nonce, born + ERA_PERIOD)
        except (Exception, KeyboardInterrupt) as e:  # Ctrl-C included: nothing is broadcast yet
            raise NotSent(str(e) or type(e).__name__) from e
        # the driver resends the same bytes after a reply timeout, and a 1010 (stale) to that resend can follow the
        # first send's acceptance: one send for this watch, so a 1010 always answers the only send
        retries = getattr(substrate, "max_retries", None)
        if retries is not None:
            substrate.max_retries = 1
        try:
            receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        except Exception as e:
            refusal = _node_refusal(e)
            if refusal is None:  # the outcome is not proved: it may still land, its record stays
                raise
            _forget(coldkey_ss58, entry)  # the node refused it: nothing is in its pool, nothing can land
            raise Refused(refusal) from e
        finally:
            if retries is not None:
                substrate.max_retries = retries
        _forget(coldkey_ss58, entry)
        return receipt.is_success, None if receipt.is_success else receipt.error_message

    def mint() -> tuple[bool, str | None]:
        return submit([("mint_gamma", params)] + ([("spend_gamma", spend)] if spec_493 else []), nonce)

    try:
        ok, error = ui.load("Minting gamma", mint)
    except Refused as e:
        raise CliFailure("transfer_failed", f"Gamma mint failed: {e}", EXIT_GENERAL_ERROR, hint=REFUSED_HINT) from e
    except NotSent as e:  # failed before signing: nothing was broadcast, nothing to wait for
        raise CliFailure("transfer_failed", f"Gamma mint failed: {e}", EXIT_GENERAL_ERROR, hint=NOT_SENT_HINT) from e
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - may still land: its record (written before) stays
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
                except NotSent:  # not broadcast, or refused by the node: nothing is pending
                    raise
                except (Exception, KeyboardInterrupt):  # sent, but the answer was lost
                    rest_pending = rest  # recorded before broadcast: the next run waits until it settles
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
