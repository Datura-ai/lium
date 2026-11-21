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
from contextlib import contextmanager
from decimal import ROUND_CEILING, Decimal
from hashlib import blake2b
from pathlib import Path

import click

from lium.cli import ui
from lium.cli.utils import EXIT_GENERAL_ERROR, CliFailure, try_lock
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
REST_HINT = (
    "Gamma left in the deposit is spent with your next top-up, or credited without minting under Credit the rest of "
    "my deposit in Billing, Add funds. Run this again only to add more."
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


def _spec_493(subtensor, block=None) -> bool:
    """Deposits have no spender, read from the connected runtime (at ``block``, the head when None), never the
    installed SDK: bittensor <= 10 reads ``mint_gamma``'s fields from the call metadata; bittensor 11, which exposes
    no call metadata, reads ``GammaSpent``, the storage runtime 493 adds with it (its codec raises
    ``StorageFunctionNotFound`` for an item the runtime lacks)."""
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        call = substrate.get_metadata_call_function("SubtensorModule", "mint_gamma", block_hash=block)
        return "spender" not in {field["name"] for field in getattr(call, "value", call)["fields"]}
    try:
        subtensor.query(("SubtensorModule", "GammaSpent"), [NETUID, ZERO_ACCOUNT, ZERO_ACCOUNT], block=block)
    except Exception as e:  # only the runtime lacking the item means "before 493"
        if any(cls.__name__ == "StorageFunctionNotFound" for cls in type(e).__mro__):
            return False
        raise
    return True


RUNTIME_CHANGED = "the chain's runtime changed while this command ran; run the command again"


def _pinned_runtime(subtensor) -> tuple[object, int, int, bool]:
    """One finalized block (as _finalized gives it), its runtime's spec version and whether that runtime is 493, all
    read at that block: a payment is decided, composed and signed against this one runtime."""
    block, number = _finalized(subtensor)
    substrate = getattr(subtensor, "substrate", None)
    if substrate is not None:
        spec_version = substrate.init_runtime(block_hash=block).runtime_version
    else:
        spec_version = subtensor.query(("System", "LastRuntimeUpgrade"), [], block=number)["spec_version"]
    return block, number, int(spec_version), _spec_493(subtensor, block if substrate is not None else number)


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


# one file per coldkey, {"nonce", "hash", "born", "dies"}: the signed payment whose outcome is not known yet, its
# extrinsic hash and the finalized blocks of its mortal era (it can land from "born" to "dies"). Written before
# broadcasting and kept across runs until finalized state resolves it: its nonce used (then its dispatch result is read
# from the block that carries it) or finalized blocks passing its era; never wall time, so a stalled chain keeps it
# unresolved. Replaced atomically; a payment holds the coldkey's lock file (_paying) from its nonce check to its outcome,
# so no other run reads, records or clears the record meanwhile. Older records carry only nonce and dies.
UNRESOLVED = Path.home() / ".lium" / "gamma_unresolved"
ERA_PERIOD = 128  # blocks a signed payment stays valid (bittensor 11's default; ~26 min at 12 s)


class OutcomeUnknown(Exception):
    """Broadcast, but the answer was lost: the transaction may still land."""


class NotSent(Exception):
    """Failed before anything was signed and broadcast (composing or signing): nothing can land."""


class Refused(NotSent):
    """The node refused the transaction on its only send: it never entered the pool, so it cannot land."""


@contextmanager
def _paying(address: str):
    """One payment per coldkey at a time, across processes: held from the nonce check until the outcome is handled, so a
    run can only clear the record of the payment it sent."""
    UNRESOLVED.mkdir(parents=True, exist_ok=True)
    fd = os.open(UNRESOLVED / f"{address}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not try_lock(fd):
            raise LiumError(
                f"Another 'lium fund --gamma' from {address} is running. Run this again once it has finished."
            )
        yield
    finally:
        os.close(fd)


@contextmanager
def _one_send(substrate):
    """bittensor <= 10's driver resends a watched extrinsic after a reply timeout, and a 1010 (stale) to that resend can
    follow the first send's acceptance: the watch is sent once, so a 1010 always answers the only send. The driver's
    author_unwatchExtrinsic cleanup, made inside the watch once the outcome arrived, keeps the driver's own retries, and
    its failure never replaces that outcome."""
    retries = getattr(substrate, "max_retries", None)
    rpc_request = getattr(substrate, "rpc_request", None)
    if retries is None or rpc_request is None:  # not the retrying driver
        yield
        return
    shadowed = vars(substrate).get("rpc_request")

    def cleanup_tolerant(method, params, *args, **kwargs):
        if method != "author_unwatchExtrinsic":
            return rpc_request(method, params, *args, **kwargs)
        substrate.max_retries = retries
        try:
            return rpc_request(method, params, *args, **kwargs)
        except Exception:  # noqa: BLE001 - cleanup only: the watched outcome already arrived
            return {}
        finally:
            substrate.max_retries = 1

    substrate.max_retries, substrate.rpc_request = 1, cleanup_tolerant
    try:
        yield
    finally:
        substrate.max_retries = retries
        if shadowed is None:
            del substrate.rpc_request
        else:
            substrate.rpc_request = shadowed


def _gamma(units: int) -> str:
    """``units`` as gamma, exact: never rounded below what a payment needs."""
    return f"{Decimal(units) / _UNITS:f}"


def _recorded(address: str) -> dict | None:
    """The coldkey's unresolved payment, or None. An unreadable record refuses: it may hide a payment that can land."""
    path = UNRESOLVED / f"{address}.json"
    try:
        entry = json.loads(path.read_text())
        known = {"nonce": int(entry["nonce"]), "dies": int(entry["dies"])}
        if "hash" in entry:
            known.update(hash=str(entry["hash"]), born=int(entry["born"]), rest=bool(entry.get("rest")))
        else:
            # an older lium's record names no transaction: it was written right after reading finalized block
            # dies - ERA_PERIOD and before broadcasting, so the payment can only be in a later block (found by nonce)
            known["born"] = known["dies"] - ERA_PERIOD
        return known
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise LiumError(
            f"Cannot read {path} ({e}), the record of a gamma payment from {address} whose outcome is unknown. "
            "Check the wallet's recent transactions, then delete the file to pay again."
        ) from e


def _record(address: str, nonce: int, tx_hash: str, born: int, rest: bool = False) -> dict:
    """Records the one payment in flight (_settled_nonce resolves any earlier one before a run signs). ``rest`` marks
    the separate spend of what a top-up left in the deposit: that top-up has paid, whatever becomes of the spend."""
    entry = {"nonce": nonce, "dies": born + ERA_PERIOD, "hash": tx_hash, "born": born, "rest": rest}
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


def _compact(n: int) -> bytes:
    """SCALE compact encoding of a length (a payment is far below 2**30 bytes)."""
    if n < 1 << 6:
        return bytes([n << 2])
    return (n << 2 | 1).to_bytes(2, "little") if n < 1 << 14 else (n << 2 | 2).to_bytes(4, "little")


def _signed_hash(unsigned, signature: bytes) -> str:
    """The extrinsic hash of ``unsigned`` (bittensor 11) once signed with ``signature``, as the chain reports it: blake2b-256
    of the length-prefixed v4 extrinsic (signer as MultiAddress::Id, MultiSignature, extensions, call).

    shortcut: assembles the v4 layout bittensor 11 sends (checked against its encoder); read the hash from bittensor
    once a public API returns it before broadcasting."""
    crypto_type = unsigned.crypto_type
    if len(signature) == 65:  # carries its signature version first
        crypto_type, signature = signature[0], signature[1:]
    body = b"\x84\x00" + bytes(unsigned.public_key) + bytes([crypto_type]) + bytes(signature)
    body += bytes(unsigned.included_in_extrinsic) + bytes(unsigned.call_data)
    return "0x" + blake2b(_compact(len(body)) + body, digest_size=32).hexdigest()


def _hash_of(extrinsic) -> str | None:
    """A block extrinsic's hash as recorded (``0x`` hex): bittensor <= 10 decodes each into an object whose
    ``extrinsic_hash`` property holds the bytes (None for inherents), the way its receipts find a transaction;
    bittensor 11 gives the plain decoded value (None when undecodable) with the hash in it."""
    found = extrinsic.get("extrinsic_hash") if isinstance(extrinsic, dict) else getattr(extrinsic, "extrinsic_hash", None)
    return "0x" + bytes(found).hex() if isinstance(found, (bytes, bytearray)) else found


def _v11_refused_names() -> set[str]:
    """The pool-rejection names that prove a bittensor 11 payment never entered the pool, read from the SDK's own map.
    Read before anything is signed: an SDK without the map refuses here, not after a payment is sent."""
    try:
        from bittensor.error_map import CUSTOM_TRANSACTION_ERRORS, INVALID_TRANSACTION_ERRORS
    except ImportError as e:
        raise LiumError(
            f"This bittensor ({e.name or 'bittensor.error_map'} missing) cannot tell a refused payment from a pending "
            "one, so lium fund --gamma does not pay with it. Install bittensor 10, or 11.3 or later. Nothing was signed."
        ) from e
    # 1013/1014 are the pool's (it may hold the payment); Stale also answers a resend of a payment already accepted
    unproved = {"AlreadyImported", "PriorityTooLow", "Stale"}
    return {name for _, name in INVALID_TRANSACTION_ERRORS} - unproved | set(CUSTOM_TRANSACTION_ERRORS.values())


def _v11_refusal(resp, refused: set[str]) -> str | None:
    """bittensor 11's failed result that proves the payment can never land, or None when its outcome is not proved.

    A dispatch result (``block_hash`` set) is the chain's answer, not a refusal. Before inclusion, bittensor 11.3.0
    returns the same ``success=False`` for the node refusing the transaction and for a watch that ended without
    finality, so only an invalid-transaction refusal (1010, named by the SDK's own pool-rejection map, as
    ``_node_refusal`` reads bittensor 10's) proves it never entered the pool."""
    if getattr(resp, "block_hash", None) or (getattr(resp, "data", None) or {}).get("submission_outcome_unknown"):
        return None
    return (resp.message or "refused") if getattr(getattr(resp, "error", None), "name", None) in refused else None


def _public_key(ss58: str) -> str:
    """The ``0x`` hex account id of a one-byte-prefix SS58 address (Bittensor's 42): prefix, 32 bytes, checksum."""
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    number = 0
    for char in ss58:
        number = number * 58 + alphabet.index(char)
    raw = number.to_bytes(35, "big")
    return "0x" + raw[1:33].hex()


def _signed_with(extrinsic, address: str, nonce: int) -> bool:
    """Whether a block extrinsic is ``address``'s signed transaction at ``nonce`` (an older record's payment: at most
    one transaction of an account carries a nonce). Unsigned ones are not; one whose signer or nonce cannot be read
    raises, so a payment is never taken for absent."""
    value = extrinsic if isinstance(extrinsic, dict) or extrinsic is None else getattr(extrinsic, "value", None)
    if isinstance(value, dict) and not value.get("address"):
        return False  # an inherent or other unsigned extrinsic
    signer = value.get("address") if isinstance(value, dict) else None
    if isinstance(signer, dict):  # MultiAddress decoded as {"Id": ...}
        signer = signer.get("Id")
    if not isinstance(signer, str) or value.get("nonce") is None:
        raise LiumError(
            f"A transaction in the era of the earlier gamma payment from {address} (nonce {nonce}) cannot be read, so "
            "whether that payment reached the chain is not known. Its record is kept. Run this again with an archive "
            "endpoint (BT_SUBTENSOR_NETWORK=archive). Nothing new was signed."
        )
    if int(value["nonce"]) != nonce:
        return False
    return signer == address or signer.startswith("0x") and signer.lower() == _public_key(address)


def _landed(subtensor, address: str, recorded: dict, number: int) -> tuple[int, bool, str | None] | None:
    """(block, succeeded, error) of the recorded payment in finalized blocks ``born`` to ``number`` (within its era),
    None when none of them carries it. A carried payment whose dispatch result cannot be read raises: it stays recorded.
    A record with a hash is found by it; an older one by its signer and nonce."""
    label = recorded.get("hash") or f"(nonce {recorded['nonce']})"
    substrate = getattr(subtensor, "substrate", None)
    for block in range(recorded["born"], min(recorded["dies"], number) + 1):
        # a node without this block's history answers no hash (the SDK would then read the tip) or fails the read:
        # the scan is incomplete, so nothing about the payment is proved
        try:
            if substrate is not None:
                block_hash = substrate.get_block_hash(block)
                extrinsics = substrate.get_extrinsics(block_hash=block_hash) if block_hash else None
            else:
                info = subtensor.block_info(block)
                extrinsics = info.extrinsics if info is not None else None
        except Exception:
            extrinsics = None
        if extrinsics is None:
            raise LiumError(
                f"The chain endpoint has no history for block {block}, so whether the earlier gamma payment "
                f"{label} reached the chain cannot be read. It can still land until block "
                f"{recorded['dies']}, so its record is kept. Run this again with an archive endpoint "
                "(BT_SUBTENSOR_NETWORK=archive), which reads the chain and settles it. Nothing new was signed."
            )
        if "hash" in recorded:
            hashes = [_hash_of(e) for e in extrinsics]
            index = hashes.index(recorded["hash"]) if recorded["hash"] in hashes else None
        else:
            found = [i for i, e in enumerate(extrinsics) if _signed_with(e, address, recorded["nonce"])]
            index = found[0] if found else None
        if index is None:
            continue
        if substrate is not None:
            events = substrate.get_events(block_hash)
        else:
            events = subtensor.query(("System", "Events"), [], block=block)
        for record in events or []:
            event = record.get("event") or record  # bittensor <= 10 nests the event in its record
            if record.get("extrinsic_idx") == index and event.get("module_id") == "System":
                if event.get("event_id") == "ExtrinsicSuccess":
                    return block, True, None
                if event.get("event_id") == "ExtrinsicFailed":
                    return block, False, str((event.get("attributes") or {}).get("dispatch_error"))
        raise LiumError(
            f"The earlier gamma payment {label} is in block {block}, but its result could not be read. "
            "Run this again in a minute."
        )
    return None


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
    if int(pool) > final:
        raise LiumError(
            f"An earlier transaction from {address} has not settled yet (its outcome was unknown or it is "
            "still in the pool). Run this again once it is finalized, usually within a minute."
        )
    if recorded is None:
        return final
    # the account nonce proves nothing about a recorded payment (an emptied and refilled account restarts it at 0):
    # only the chain's blocks in its era do
    landed = _landed(subtensor, address, recorded, number)
    if landed is None:
        if number <= recorded["dies"]:  # it can still land until its era ends
            raise LiumError(
                f"An earlier gamma payment from {address} has not settled yet: it can still reach the chain until "
                f"block {recorded['dies']} (finalized now: {number}). Run this again after that block. Nothing new "
                "was signed."
            )
        _forget(address, recorded)
        _stop_unless_a_topup(address, recorded, "never reached the chain")
        ui.warning(f"An earlier gamma payment from {address} never reached the chain; it paid nothing.")
        return final
    _forget(address, recorded)
    block, ok, error = landed
    if ok:
        raise CliFailure(
            "gamma_payment_landed",
            f"An earlier gamma payment from {address} whose answer was lost succeeded in block {block}. "
            "Nothing new was signed.",
            EXIT_GENERAL_ERROR,
            hint="Lium credits it within a few minutes: check 'lium balance', then run this again only to add more.",
        )
    _stop_unless_a_topup(address, recorded, f"failed in block {block} ({error})")
    ui.warning(f"An earlier gamma payment from {address} failed in block {block} ({error}); it paid nothing.")
    return final


def _stop_unless_a_topup(address: str, recorded: dict, outcome: str) -> None:
    """A spend of what a top-up left in the deposit that did not land says the top-up itself paid, so it is not a
    payment that paid nothing: the run stops, signing nothing, rather than mint a whole new amount. An older lium's
    record does not say which of the two it was, so it stops the same way, once (its record is already dropped)."""
    if recorded.get("rest"):
        raise CliFailure(
            "gamma_rest_not_spent",
            f"An earlier top-up from {address} paid. The later spend of the gamma it left in the deposit {outcome}, "
            "so that gamma stays in the deposit. Nothing new was signed.",
            EXIT_GENERAL_ERROR,
            hint=REST_HINT,
        )
    if "hash" not in recorded:
        raise CliFailure(
            "gamma_payment_unclear",
            f"An earlier gamma payment from {address} (nonce {recorded['nonce']}) {outcome}. It was recorded by an "
            "older lium, which did not note whether it was a top-up or the later spend of the gamma a top-up left in "
            "the deposit. If it was a top-up, it paid nothing. If it was that later spend, the top-up before it paid "
            "and the gamma stays in the deposit. Nothing new was signed.",
            EXIT_GENERAL_ERROR,
            hint="Check 'lium balance' for that top-up before paying again. " + REST_HINT,
        )


def gamma_fund(bt, bt_wallet, coldkey_ss58: str, wallet: str, usd: float, yes: bool, json_output: bool) -> None:
    with _paying(coldkey_ss58):
        _fund(bt, bt_wallet, coldkey_ss58, wallet, usd, yes, json_output)


def _fund(bt, bt_wallet, coldkey_ss58: str, wallet: str, usd: float, yes: bool, json_output: bool) -> None:
    from .actions import UnlockColdkeyAction

    lium = Lium()  # before the unlock: a missing API key never costs a password prompt
    # bittensor 11 reads no network from the environment: the CLI passes BT_SUBTENSOR_NETWORK itself, so the
    # archive endpoint named in the unresolved-payment message works on every supported bittensor
    network = os.environ.get("BT_SUBTENSOR_NETWORK")
    subtensor = validation.subtensor_class(bt)(network=network) if network else validation.subtensor_class(bt)()
    gamma_per_tao = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, _UNITS))
    tao_rao = tao_rao_for_usd(usd, gamma_per_tao)
    expected = ui.load("Quoting gamma", lambda: _sim_gamma(subtensor, tao_rao))
    min_out = min_gamma_out(expected)
    tao = tao_rao / _UNITS
    # an earlier mint or spend whose outcome was unknown must settle first: until then the deposit read
    # below may still count gamma it spends, and a payment built on it would fail or be paid twice
    nonce = ui.load("Checking for unsettled transactions", lambda: _settled_nonce(subtensor, coldkey_ss58))
    spec_493 = _spec_493(subtensor)
    def spendable() -> int:
        """Gamma units the deposit holds now; refused before anything is signed when its daily spend cap cannot
        spend them with the mint's minimum."""
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
                    wait = f"Or wait about {hours:.1f} h for the cap to refill to {_gamma(need)} gamma."
                raise CliFailure(
                    "gamma_spend_cap",
                    f"The gamma deposit at {coldkey_ss58} has a daily spend cap of {_gamma(cap)} gamma and can spend "
                    f"{_gamma(allows)} gamma now; this payment spends {_gamma(need)} gamma (at least "
                    f"{_gamma(min_out)} minted plus {_gamma(held)} already in the deposit), so the chain would refuse it.",
                    EXIT_GENERAL_ERROR,
                    hint=(
                        f"Remove the cap with set_gamma_deposit_daily_spend_cap (SN51, hotkey {coldkey_ss58}, signed by "
                        "this coldkey), then run this again at once. Raising the cap instead (to at least "
                        f"{_gamma(need)} gamma) does not add to what the deposit can spend now: wait for it to refill "
                        f"before running this again. {wait} Nothing was signed or sent."
                    ),
                )
        return held

    refused = _v11_refused_names() if getattr(subtensor, "substrate", None) is None else set()
    spendable()  # refuses before the password prompt; checked again after linking

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
    # an Any proxy of the coldkey can spend the deposit meanwhile, with its own nonce: the payment is checked and
    # sized from what the deposit holds now
    held = spendable()
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

    def submit(calls: list[tuple[str, dict]], nonce: int, rest: bool = False) -> tuple[bool, str | None]:
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
                _, number, spec_version, pinned_493 = _pinned_runtime(subtensor)
                if pinned_493 != spec_493:  # the calls were chosen for another runtime
                    raise NotSent(RUNTIME_CHANGED)
                # the nested calls compose at the head: under another runtime than the pinned one they do not encode
                # (a 493 batch has no spend_gamma, nor a spender-less mint_gamma, before 493), so nothing is signed
                call = built[0] if len(built) == 1 else ("Utility", "batch_all", {"calls": [subtensor.compose(c) for c in built]})
                unsigned = subtensor.prepare_call(call, address=coldkey_ss58, nonce=nonce, period=ERA_PERIOD)
                if unsigned.spec_version != spec_version:  # the signature commits to the spec version it is made for
                    raise NotSent(RUNTIME_CHANGED)
                signature = coldkey.sign(unsigned.payload)
                entry = _record(coldkey_ss58, nonce, _signed_hash(unsigned, signature), int(unsigned.era["current"]), rest)
            except (Exception, KeyboardInterrupt) as e:  # Ctrl-C included: nothing is broadcast yet
                raise NotSent(str(e) or type(e).__name__) from e
            resp = subtensor.submit_signature(unsigned, signature, wait_for_inclusion=True, wait_for_finalization=True)
            # the record goes only on success or a proved refusal: in a block, success=False is also what bittensor
            # 11.3.0 answers when it read no dispatch events, so a failure is left to the next run's read of the block
            if not resp.success:
                refusal = None if getattr(resp, "block_hash", None) else _v11_refusal(resp, refused)
                if refusal is None:  # e.g. the watch ended before finality, or the result was unreadable: it may have paid
                    raise OutcomeUnknown(resp.message or "submission outcome unknown")
                _forget(coldkey_ss58, entry)
                raise Refused(refusal)
            _forget(coldkey_ss58, entry)
            return True, None
        try:
            # the era starts at this finalized block, not the best head; the calls compose with its metadata
            block, born, spec_version, pinned_493 = _pinned_runtime(subtensor)
            if pinned_493 != spec_493:  # the calls were chosen for another runtime
                raise NotSent(RUNTIME_CHANGED)
            built = [
                substrate.compose_call(
                    call_module="SubtensorModule", call_function=function, call_params=args, block_hash=block
                )
                for function, args in calls
            ]
            call = built[0] if len(built) == 1 else substrate.compose_call(
                call_module="Utility", call_function="batch_all", call_params={"calls": built}, block_hash=block
            )
            extrinsic = substrate.create_signed_extrinsic(
                call=call, keypair=coldkey, nonce=nonce, era={"period": ERA_PERIOD, "current": born}
            )
            # signed with the runtime the driver loaded for it, whose spec version the signature commits to: the
            # chain refuses it under any other, so the composed bytes are only ever executed by the pinned runtime
            if substrate.runtime.runtime_version != spec_version:
                raise NotSent(RUNTIME_CHANGED)
            entry = _record(coldkey_ss58, nonce, "0x" + bytes(extrinsic.extrinsic_hash).hex(), born, rest)
        except (Exception, KeyboardInterrupt) as e:  # Ctrl-C included: nothing is broadcast yet
            raise NotSent(str(e) or type(e).__name__) from e
        try:
            with _one_send(substrate):
                receipt = substrate.submit_extrinsic(extrinsic, wait_for_inclusion=True, wait_for_finalization=True)
        except Exception as e:
            refusal = _node_refusal(e)
            if refusal is None:  # the outcome is not proved: it may still land, its record stays
                raise
            _forget(coldkey_ss58, entry)  # the node refused it: nothing is in its pool, nothing can land
            raise Refused(refusal) from e
        # bittensor <= 10 reads the result lazily: the record stays until it is read
        ok = receipt.is_success
        if ok is None:  # finalized, but its dispatch events were not readable: it may have paid
            raise OutcomeUnknown("the payment's result could not be read")
        error = None if ok else receipt.error_message
        _forget(coldkey_ss58, entry)
        return ok, error

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
                        lambda: submit(
                            [("spend_gamma", {"service": payee, "netuid": NETUID, "amount": rest})], nonce + 1, rest=True
                        ),
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
