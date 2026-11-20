"""DAH-6385: ``lium fund --gamma`` mints SN51 gamma into a linked deposit Lium credits as USD."""

import json
import os
import sys
import types

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.fund import actions as actions_module
from lium.cli.fund import command as fund_module
from lium.cli.fund import gamma as gamma_module
from lium.cli.utils import try_lock

GAMMA_PER_TAO = 210_561_193_201  # sim_mint_gamma(51, 1 TAO) on finney, 13 Nov 2026


@pytest.fixture(autouse=True)
def _unresolved_file(monkeypatch, tmp_path):
    monkeypatch.setattr(gamma_module, "UNRESOLVED", tmp_path / "unresolved")


def test_usd_is_sized_to_tao_rounded_up_and_floored_two_percent():
    rao = gamma_module.tao_rao_for_usd(25, GAMMA_PER_TAO)

    assert rao * GAMMA_PER_TAO >= 25 * 10**18 > (rao - 1) * GAMMA_PER_TAO
    assert gamma_module.min_gamma_out(25 * 10**9) == 24_500_000_000


class FinalizedBlocks:
    """Finalized blocks that carry a payment: ``blocks[number] = [(extrinsic hash, System event or None)]``."""

    def _block(self, number):
        assert number <= getattr(self, "finalized_number", getattr(self, "finalized_block", None))  # finalized only
        return self.blocks.get(number, [])

    def _events(self, number):
        return [{"extrinsic_idx": i, "module_id": "System", "event_id": event,
                 "attributes": {"dispatch_error": {"Module": "Slippage"}} if event == "ExtrinsicFailed" else {}}
                for i, (_, event) in enumerate(self._block(number)) if event]

    def _event_records(self, number):
        """bittensor <= 10's locked async-substrate-interface 2.0.3: each record nests its event (its receipts read
        ``event["event"]["module_id"]``)."""
        return [{"extrinsic_idx": e.pop("extrinsic_idx"), "event": e} for e in self._events(number)]


class Signed(tuple):
    """A signed extrinsic: (call, keypair), with the hash the chain knows it by."""


class FakeSubstrate(FinalizedBlocks):
    """bittensor <= 10: ``Subtensor().substrate`` composes, signs and submits."""

    def __init__(self, success=True, submit=None, spec_493=False, held=None):
        self.success, self.calls, self.submitted, self.submit = success, [], None, submit
        # held: the deposit balance at each read (before the batch, then after it), None for no deposit
        self.spec_493, self.held, self.submissions = spec_493, list(held or [None, None]), []
        self.nonces, self.final_nonce, self.finalized_number, self.blocks = [], 7, 100, {}

    pending = False  # a transaction from the coldkey still in the pool
    cap = {}  # {"daily_spend_cap": {"cap", "bucket": (units, block)}} for a capped deposit
    # the runtime at the finalized block the payment is pinned to, and the one the driver signs with (the head's);
    # None: the same as spec_493
    pinned_493 = signed_493 = None

    def _at(self, block_hash):
        return self.spec_493 if block_hash is None or self.pinned_493 is None else self.pinned_493

    def init_runtime(self, block_hash):
        assert block_hash == "0xfinal"
        self.runtime = types.SimpleNamespace(runtime_version=493 if self._at(block_hash) else 492)
        return self.runtime

    def get_account_next_index(self, address):
        assert address == "5Cold"
        return self.final_nonce + (1 if self.pending else 0)

    def get_metadata_call_function(self, module, function, block_hash=None):
        # the shape finney's metadata returns: a Variant whose value lists the call's fields
        assert (module, function) == ("SubtensorModule", "mint_gamma")
        names = ["hotkey", "netuid", "amount", "min_amount_out"] + ([] if self._at(block_hash) else ["spender"])
        return types.SimpleNamespace(value={"name": "mint_gamma", "fields": [{"name": n} for n in names + ["daily_spend_cap"]]})

    def query(self, module, storage, params, block_hash=None):
        if (module, storage) == ("System", "Account"):
            assert block_hash == "0xfinal"  # settled means finalized
            return types.SimpleNamespace(value={"nonce": self.final_nonce})
        assert (module, storage, params) == ("SubtensorModule", "GammaDeposits", [51, "5Cold"])
        held = self.held[1 if self.submissions else 0]
        return types.SimpleNamespace(value=None if held is None else {"coldkey": "5Cold", "balance": held, **self.cap})

    def runtime_call(self, api, method, params):
        assert (api, method, params[0]) == ("GammaRuntimeApi", "sim_mint_gamma", 51)
        self.calls.append(params[1])
        return types.SimpleNamespace(value={"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)})

    def compose_call(self, call_module, call_function, call_params, block_hash=None):
        assert block_hash == "0xfinal"  # composed with the pinned block's metadata, never the head's
        if call_function == "mint_gamma":  # the metadata decides the encoding: an obsolete field is dropped
            assert ("spender" in call_params) != self._at(block_hash)
        return (call_module, call_function, call_params)

    def create_signed_extrinsic(self, call, keypair, nonce, era):
        assert era == {"period": gamma_module.ERA_PERIOD, "current": self.finalized_number}
        signed = self.spec_493 if self.signed_493 is None else self.signed_493
        self.runtime = types.SimpleNamespace(runtime_version=493 if signed else 492)  # the driver loads the head's
        self.nonces.append(nonce)
        signed = Signed((call, keypair))
        signed.extrinsic_hash = bytes([nonce]) * 32
        return signed

    def get_block_hash(self, number):
        return f"0xblock{number}"

    def get_extrinsics(self, block_hash):
        # async-substrate-interface 2.0.3: the hash is the decoded extrinsic's property (bytes), not in its value
        return [types.SimpleNamespace(extrinsic_hash=bytes.fromhex(h[2:]), value={"call": {}})
                for h, _ in self._block(int(block_hash[7:]))]

    def get_events(self, block_hash):
        return self._event_records(int(block_hash[7:]))

    def get_chain_finalised_head(self):
        return "0xfinal"

    def get_block_number(self, block_hash):
        assert block_hash == "0xfinal"
        return self.finalized_number

    def submit_extrinsic(self, extrinsic, wait_for_inclusion, wait_for_finalization):
        assert wait_for_inclusion and wait_for_finalization
        if self.submit:
            return self.submit(extrinsic)
        self.submissions.append(extrinsic)
        self.submitted = self.submitted or extrinsic
        return types.SimpleNamespace(is_success=self.success, error_message=None if self.success else "Slippage")

    def subtensor(self):
        return types.SimpleNamespace(substrate=self)


SIGNED = "signed by the coldkey"


class FakeV11Subtensor(FinalizedBlocks):
    """bittensor 11: no ``.substrate``; ``runtime`` reads and ``submit_call`` sends a ``bt.calls`` builder."""

    def __init__(self, success=True, held=None, spec_493=False):
        self.success, self.submitted, self.held, self.submissions = success, None, list(held or [None, None]), []
        self.spec_493 = spec_493  # the connected runtime, whatever the installed SDK was generated for
        self.nonces, self.final_nonce, self.finalized_block, self.unknown = [], 7, 100, False
        self.blocks = {}

    pending = False
    cap = {}
    pinned_493 = signed_493 = None  # as FakeSubstrate's

    def _at(self, block):
        return self.spec_493 if block is None or self.pinned_493 is None else self.pinned_493

    def compose(self, call):
        _, function, params = call
        # the runtime refuses fields it does not have: a builder from another runtime cannot slip through
        assert ("spender" in params) != self.spec_493 if function == "mint_gamma" else True
        return ("composed", call)

    def prepare_call(self, call, address, nonce=None, period=None):
        assert address == "5Cold"
        if nonce is None:  # the pool's next nonce
            assert call == ("Utility", "batch_all", {"calls": []})
            return types.SimpleNamespace(nonce=self.final_nonce + (1 if self.pending else 0))
        assert period == gamma_module.ERA_PERIOD
        signed = self.spec_493 if self.signed_493 is None else self.signed_493
        return types.SimpleNamespace(
            nonce=nonce, payload=b"payload", call=call, spec_version=493 if signed else 492,
            era={"period": period, "current": self.finalized_block}, public_key=b"\x05" * 32, crypto_type=1,
            included_in_extrinsic=bytes([nonce]), call_data=repr(call).encode(),
        )

    def query(self, item, params, block=None):
        if item == ("System", "Account"):
            assert block == self.finalized_block  # settled means finalized
            return {"nonce": self.final_nonce}
        if item == ("System", "Events"):
            return self._events(block)
        if item == ("System", "LastRuntimeUpgrade"):
            assert block == self.finalized_block
            return {"spec_version": 493 if self._at(block) else 492, "spec_name": "node-subtensor"}
        if item == ("SubtensorModule", "GammaSpent"):
            if not self._at(block):
                raise type("StorageFunctionNotFound", (ValueError,), {})("SubtensorModule.GammaSpent")
            return 0
        assert (item, params) == (("SubtensorModule", "GammaDeposits"), [51, "5Cold"])
        held = self.held[1 if self.submissions else 0]
        return None if held is None else {"coldkey": "5Cold", "balance": held, **self.cap}

    def block_info(self, number):
        return types.SimpleNamespace(extrinsics=[{"extrinsic_hash": h} for h, _ in self._block(number)])

    def runtime(self, method, params):
        assert method == ("GammaRuntimeApi", "sim_mint_gamma") and params[0] == 51
        return {"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)}

    def submit_signature(self, unsigned, signature, wait_for_inclusion, wait_for_finalization):
        # signed by the unlocked keypair itself (the fake coldkey signs b"\x01\x02"): never a second unlock
        assert wait_for_inclusion and wait_for_finalization and signature == b"\x01\x02"
        self.nonces.append(unsigned.nonce)
        if self.unknown and self.submissions:  # the rest spend: broadcast, answer lost
            return types.SimpleNamespace(success=False, message="disconnected", data={"submission_outcome_unknown": True})
        self.submissions.append((tuple(unsigned.call), SIGNED))
        self.submitted = self.submitted or self.submissions[-1]
        return types.SimpleNamespace(success=self.success, message="" if self.success else "Slippage", data={},
                                     block_hash="0xincluded", error=None)

    def subtensor(self):
        return self


def _calls(spec_493=False):
    """bittensor 11's generated builders, as built for the runtime before or from 493 (no spender)."""
    def mint(hotkey, netuid, amount, min_amount_out, spender, daily_spend_cap):
        return ("SubtensorModule", "mint_gamma", dict(locals()))

    def mint_493(hotkey, netuid, amount, min_amount_out, daily_spend_cap):
        return ("SubtensorModule", "mint_gamma", dict(locals()))

    def spend(service, netuid, amount):
        return ("SubtensorModule", "spend_gamma", dict(locals()))

    return types.SimpleNamespace(
        SubtensorModule=types.SimpleNamespace(mint_gamma=mint_493 if spec_493 else mint, spend_gamma=spend),
        Utility=types.SimpleNamespace(batch_all=lambda calls: ("Utility", "batch_all", {"calls": calls})),
    )


# bittensor 11.3's pool-rejection names (bittensor/error_map.py), the part the CLI reads
ERROR_MAP = types.SimpleNamespace(
    INVALID_TRANSACTION_ERRORS=(("inability to pay some fees", "Payment"), ("transaction is outdated", "Stale"),
                                ("already imported", "AlreadyImported"), ("priority is too low", "PriorityTooLow")),
    CUSTOM_TRANSACTION_ERRORS={1: "ColdKeyInSwapSchedule"},
)


def _run(monkeypatch, args, substrate, calls=None, on_link=None, error_map=ERROR_MAP):
    events = []
    coldkey = types.SimpleNamespace(sign=lambda m: (events.append(("sign", m)), b"\x01\x02")[1])
    wallet = types.SimpleNamespace(coldkey=coldkey, unlock_coldkey=lambda: events.append("unlock"))
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace(Subtensor=substrate.subtensor, calls=calls or _calls()))
    monkeypatch.setitem(sys.modules, "bittensor.error_map", error_map)  # None: the import fails

    class FakeLoadWalletAction:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"wallet": wallet, "address": "5Cold"})

    class FakeLium:
        def link_gamma_hotkey(self, hotkey, sign):
            events.append(("link", hotkey, sign("challenge")))
            if on_link:
                on_link()
            return {"hotkey": hotkey, "netuid": 51, "spender": "5Spender", "service": "5Service"}

    monkeypatch.setattr(fund_module, "LoadWalletAction", FakeLoadWalletAction)
    monkeypatch.setattr(gamma_module, "Lium", FakeLium)
    monkeypatch.setattr(actions_module.UnlockColdkeyAction, "execute",
                        lambda self, ctx: (ctx["bt_wallet"].unlock_coldkey(), ActionResult(ok=True, data={}))[1])
    return CliRunner().invoke(cli, args), events, coldkey


@pytest.mark.parametrize(
    "chain, sdk_493",
    [(FakeSubstrate, False), (FakeV11Subtensor, False), (FakeV11Subtensor, True)],
    ids=["bittensor<=10", "bittensor11", "bittensor11-built-for-493"],
)
def test_gamma_links_the_coldkey_and_mints_naming_the_spender(monkeypatch, chain, sdk_493):
    substrate = chain()  # a runtime before 493

    result, events, coldkey = _run(
        monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, calls=_calls(sdk_493)
    )

    assert result.exit_code == 0, result.output
    # bittensor 11 also signs the payload of the payment it prepared
    assert events == ["unlock", ("sign", "challenge"), ("link", "5Cold", "0x0102")] + (
        [] if chain is FakeSubstrate else [("sign", b"payload")]
    )
    (module, function, params), keypair = substrate.submitted
    rao = gamma_module.tao_rao_for_usd(25, GAMMA_PER_TAO)
    expected = rao * GAMMA_PER_TAO // 10**9
    assert (module, function, keypair) == ("SubtensorModule", "mint_gamma", coldkey if chain is FakeSubstrate else SIGNED)
    assert substrate.nonces == [7]  # signed with the nonce checked as settled
    assert params == {"hotkey": "5Cold", "netuid": 51, "amount": rao, "min_amount_out": expected * 98 // 100,
                      "spender": "5Spender", "daily_spend_cap": None}
    assert json.loads(result.output.strip().splitlines()[-1])["tx"]["spender"] == "5Spender"


def _lost_reply(extrinsic):
    raise ConnectionError("websocket closed")


def _interrupted(extrinsic):
    raise KeyboardInterrupt  # Ctrl-C while waiting for finalization, after the broadcast


@pytest.mark.parametrize(
    "chain, reason",
    [
        (FakeSubstrate(success=False), "Slippage"),
        (FakeV11Subtensor(success=False), "Slippage"),
        (FakeSubstrate(submit=_lost_reply), "websocket closed"),
        (FakeSubstrate(submit=_interrupted), "KeyboardInterrupt"),
    ],
    ids=["dispatch-rejected", "dispatch-rejected-v11", "lost-reply", "interrupted"],
)
def test_a_failed_mint_exits_non_zero_and_warns_against_resending(monkeypatch, chain, reason):
    result, _, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], chain)

    assert result.exit_code != 0, result.output
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert envelope["ok"] is False and reason in json.dumps(envelope) and "Do not re-send yet" in json.dumps(envelope)


def test_gamma_refuses_alpha_options(monkeypatch):
    result = CliRunner().invoke(cli, ["fund", "--gamma", "--netuid", "64", "-a", "25"])

    assert result.exit_code != 0 and "drop --alpha, --hotkey and --netuid" in result.output


# DAH-6771: from subtensor runtime 493 deposits have no spender; the coldkey (the hotkey) spends at Lium's service
# held: the deposit before the batch and after it (the mint's price margin above the minimum it spent)
HELD = [([None, None], 0, 0), ([3 * 10**9, 2 * 10**8], 3 * 10**9, 2 * 10**8)]


@pytest.mark.parametrize("held, spent_extra, rest", HELD)
def test_from_runtime_493_one_batch_mints_and_spends_at_the_service(monkeypatch, held, spent_extra, rest):
    substrate = FakeSubstrate(spec_493=True, held=held)

    result, _, coldkey = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code == 0, result.output
    (module, function, params), keypair = substrate.submitted
    assert (module, function, keypair) == ("Utility", "batch_all", coldkey)
    (_, _, mint), (_, spend_fn, spend) = params["calls"]
    min_out = mint["min_amount_out"]
    assert "spender" not in mint and mint["hotkey"] == "5Cold" and mint["daily_spend_cap"] is None
    assert (spend_fn, spend) == ("spend_gamma", {"service": "5Service", "netuid": 51, "amount": min_out + spent_extra})
    tx = json.loads(result.output.strip().splitlines()[-1])["tx"]
    assert (tx["service"], tx["gamma_rest_spent"]) == ("5Service", rest / 10**9)
    # the margin is spent right after, with the key already unlocked: the whole payment is credited
    assert substrate.submissions[1:] == (
        [(("SubtensorModule", "spend_gamma", {"service": "5Service", "netuid": 51, "amount": rest}), coldkey)]
        if rest else []
    )


@pytest.mark.parametrize("sdk_493", [True, False], ids=["built-for-493", "built-before-493"])
@pytest.mark.parametrize("held, spent_extra, rest", HELD)
def test_bittensor_11_on_runtime_493_batches_mint_and_spend(monkeypatch, held, spent_extra, rest, sdk_493):
    substrate = FakeV11Subtensor(held=held, spec_493=True)

    result, _, _ = _run(
        monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, calls=_calls(sdk_493)
    )

    assert result.exit_code == 0, result.output
    (module, function, params), keypair = substrate.submitted
    assert (module, function, keypair) == ("Utility", "batch_all", SIGNED)
    assert substrate.nonces == ([7, 8] if rest else [7])
    # composed against the connected runtime before nesting
    (tag_mint, (_, _, mint)), (tag_spend, (_, _, spend)) = params["calls"]
    assert tag_mint == tag_spend == "composed"
    assert "spender" not in mint
    assert spend == {"service": "5Service", "netuid": 51, "amount": mint["min_amount_out"] + spent_extra}
    assert [call for call, _ in substrate.submissions[1:]] == (
        [("SubtensorModule", "spend_gamma", {"service": "5Service", "netuid": 51, "amount": rest})] if rest else []
    )


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
def test_a_transaction_still_pending_stops_the_next_payment_before_anything_is_signed(monkeypatch, chain):
    substrate = chain()
    substrate.pending = True

    result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code != 0 and "has not settled yet" in result.output
    assert substrate.submissions == [] and "unlock" not in events


@pytest.mark.parametrize("lost", [_lost_reply, _interrupted], ids=["lost_reply", "interrupted"])
def test_a_rest_spend_with_an_unknown_outcome_is_reported_pending_not_unspent(monkeypatch, lost):
    def submit(extrinsic):
        if substrate.submissions:  # the rest spend, after the batch landed
            return lost(extrinsic)
        substrate.submissions.append(extrinsic)
        return types.SimpleNamespace(is_success=True, error_message=None)

    substrate = FakeSubstrate(spec_493=True, held=[None, 300_000_000], submit=submit)

    result, _, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code == 0, result.output
    assert "may still land" in result.output and "was not spent" not in result.output
    tx = json.loads(result.output.strip().splitlines()[-1])["tx"]
    assert (tx["gamma_rest_spent"], tx["gamma_rest_pending"]) == (0, 0.3)


def _gone(substrate):
    """bittensor 11 dropping the answer to the rest spend after broadcasting it."""
    substrate.unknown = True
    return substrate


@pytest.mark.parametrize(
    "make", [lambda: FakeSubstrate(spec_493=True, held=[None, 300_000_000], submit=None), None],
    ids=["bittensor<=10-raised", "bittensor11-submission-outcome-unknown"],
)
def test_an_unknown_rest_spend_blocks_the_next_payment_until_finalized_state_settles_it(monkeypatch, make):
    if make is None:
        substrate = _gone(FakeV11Subtensor(held=[None, 300_000_000], spec_493=True))
    else:
        substrate = make()

        def submit(extrinsic):
            if substrate.submissions:
                raise ConnectionError("websocket closed")
            substrate.submissions.append(extrinsic)
            return types.SimpleNamespace(is_success=True, error_message=None)

        substrate.submit = submit
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output.strip().splitlines()[-1])["tx"]["gamma_rest_pending"] == 0.3
    assert substrate.nonces[-1] == 8 and _record()["nonce"] == 8

    # the pool no longer shows it (dropped, or not yet propagated), but finalized state has not passed it
    substrate.final_nonce, done = 8, len(substrate.nonces)
    result, events, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and "has not settled yet" in result.output
    assert len(substrate.nonces) == done and "unlock" not in events

    # finalized past it: settled either way, the entry is cleared and the next payment signs above it
    substrate.final_nonce, substrate.unknown = 9, False
    substrate.held, substrate.submit = [None, None], None
    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code == 0, result.output
    assert substrate.nonces[done] == 9 and _record() is None


def test_an_unknown_mint_blocks_until_its_era_has_passed(monkeypatch):
    substrate = FakeSubstrate(submit=_lost_reply)
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and _record()["nonce"] == 7

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and "has not settled yet" in result.output

    assert _record()["dies"] == 100 + gamma_module.ERA_PERIOD

    # a stalled chain: finalized blocks reach its last valid block, however long that takes, and it may still land
    substrate.finalized_number = 100 + gamma_module.ERA_PERIOD
    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and "has not settled yet" in result.output

    # finalized past its mortal era it can no longer land: the finalized nonce is authoritative again
    substrate.finalized_number += 1
    substrate.submit = None
    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code == 0, result.output


SubstrateRequestException = type("SubstrateRequestException", (Exception,), {})  # as bittensor <= 10 raises it
MaxRetriesExceeded = type("MaxRetriesExceeded", (SubstrateRequestException,), {})


def _watch(substrate, *replies):
    """author_submitAndWatchExtrinsic on bittensor 10's locked driver (async-substrate-interface 2.0.3
    ``_make_rpc_request``): every send gets the next scripted reply; an error answer is raised as
    ``SubstrateRequestException(str(response))``; a reply timeout resends the same bytes until ``max_retries`` sends,
    then raises MaxRetriesExceeded. ``"timeout"`` scripts a send whose reply never comes."""

    def submit(extrinsic):
        for attempt in range(1, len(replies) + 1):
            substrate.watch_send_count += 1
            reply = replies[attempt - 1]
            if reply != "timeout":
                raise SubstrateRequestException(str({"jsonrpc": "2.0", "id": attempt, "error": reply}))
            if attempt >= substrate.max_retries:
                raise MaxRetriesExceeded("Max retries reached.")
        raise AssertionError("ran out of scripted replies")
    return submit


FEES = {"code": 1010, "message": "Invalid Transaction", "data": "Inability to pay some fees (e.g. account balance too low)"}
STALE = {"code": 1010, "message": "Invalid Transaction", "data": "Transaction is outdated"}


@pytest.mark.parametrize(
    "replies, definite_initial_refusal",
    [
        ((FEES,), True),  # the pool refused the only send: it never entered the pool
        (({"code": 1013, "message": "Transaction Already Imported"},), False),  # the pool holds it: it may land
        (({"code": -32603, "message": "Internal error"},), False),  # an unrecognized answer proves nothing
        (("timeout", STALE), False),  # accepted, reply lost: a resend would get a stale 1010 after acceptance
    ],
    ids=["initial-1010", "initial-1013", "unrecognized-32603", "accepted-then-timeout"],
)
def test_only_a_definite_initial_refusal_clears_the_record(monkeypatch, replies, definite_initial_refusal):
    substrate = FakeSubstrate()
    substrate.max_retries, substrate.watch_send_count = 5, 0  # the driver's default
    substrate.rpc_request = lambda method, params: {"result": True}
    substrate.submit = _watch(substrate, *replies)
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0
    assert (_record() is None) == definite_initial_refusal
    assert ("The node rejected this payment" in result.output) == definite_initial_refusal, result.output
    assert substrate.watch_send_count == 1 and substrate.max_retries == 5

    substrate.submit = None  # funded meanwhile: only a proved refusal lets the rerun sign again
    result, _, _ = _run(monkeypatch, args, substrate)
    assert (result.exit_code == 0) == definite_initial_refusal, result.output
    assert substrate.nonces == ([7, 7] if definite_initial_refusal else [7])


@pytest.mark.parametrize(
    "name, message, definite_initial_refusal",
    [
        ("Payment", "Inability to pay some fees", True),  # the node refused it: it never entered the pool
        ("ColdKeyInSwapSchedule", "Custom error: 1", True),  # a runtime's 1010 refusal, by its custom code
        (None, "Subscription ended before the extrinsic was finalized", False),  # the watch ended: it may land
        ("Dropped", "Extrinsic dropped", False),  # a watch status after the pool took it
        ("Stale", "Transaction is outdated", False),  # may answer a resend of an accepted payment
        ("AlreadyImported", "Transaction Already Imported", False),  # the pool holds it
    ],
    ids=["fee-rejection", "custom-1010", "ended-watch", "dropped", "stale", "already-imported"],
)
def test_bittensor_11_clears_the_record_only_on_a_definite_initial_refusal(
    monkeypatch, name, message, definite_initial_refusal
):
    # bittensor 11.3.0 answers both cases alike: success=False, no block, no submission_outcome_unknown
    substrate = FakeV11Subtensor()

    def failed(unsigned, signature, **kwargs):
        substrate.nonces.append(unsigned.nonce)
        return types.SimpleNamespace(success=False, message=message, data={}, block_hash=None,
                                     error=types.SimpleNamespace(name=name))

    substrate.submit_signature = failed
    result, _, _ = _run(monkeypatch, ARGS, substrate)
    assert result.exit_code != 0
    assert (_record() is None) == definite_initial_refusal
    assert ("The node rejected this payment" in result.output) == definite_initial_refusal, result.output

    del substrate.submit_signature  # funded meanwhile: only a proved refusal lets the rerun sign again
    result, _, _ = _run(monkeypatch, ARGS, substrate)
    assert (result.exit_code == 0) == definite_initial_refusal, result.output
    assert substrate.nonces == ([7, 7] if definite_initial_refusal else [7])


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
@pytest.mark.parametrize("error", [ValueError("metadata encoding failed"), KeyboardInterrupt()], ids=["error", "ctrl-c"])
def test_a_mint_that_fails_before_signing_records_nothing_and_the_next_run_proceeds(monkeypatch, chain, error):
    substrate = chain()

    def broken(*args, **kwargs):
        raise error

    original = substrate.create_signed_extrinsic if chain is FakeSubstrate else substrate.prepare_call
    if chain is FakeSubstrate:
        substrate.create_signed_extrinsic = broken
    else:
        substrate.prepare_call = lambda call, address, nonce=None, period=None: (
            original(call, address) if nonce is None else broken()
        )
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)

    assert result.exit_code == 1 and "Nothing was signed or sent" in result.output, result.output
    assert _record() is None and substrate.submissions == []
    if chain is FakeSubstrate:
        substrate.create_signed_extrinsic = original
    else:
        substrate.prepare_call = original
    assert _run(monkeypatch, args, substrate)[0].exit_code == 0



@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
def test_a_runtime_upgrade_while_linking_refuses_the_payment_before_signing(monkeypatch, chain):
    substrate = chain()  # runtime 492 when the payment is chosen

    def upgrade():
        substrate.spec_493 = True  # runtime 493 when the calls are composed

    result, events, _ = _run(
        monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, on_link=upgrade
    )

    assert result.exit_code == 1 and "runtime changed" in result.output and "Nothing was signed or sent" in result.output
    assert substrate.submissions == [] and _record() is None
    assert [e for e in events if e[0] == "sign" and e[1] != "challenge"] == []  # only the link was signed


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
@pytest.mark.parametrize(
    "pinned_493, signed_493", [(True, None), (False, True)], ids=["pinned-block-is-493", "head-signs-as-493"]
)
def test_a_payment_is_decided_composed_and_signed_against_one_runtime(monkeypatch, chain, pinned_493, signed_493):
    substrate = chain()  # the head reads 492 when the payment is chosen

    def flip():  # then the runtime differs between the pinned block and the head the driver signs with
        substrate.pinned_493, substrate.signed_493 = pinned_493, signed_493

    result, events, _ = _run(
        monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, on_link=flip
    )

    assert result.exit_code == 1 and "runtime changed" in result.output and "Nothing was signed or sent" in result.output
    # nothing reaches the chain, so no mint can land without its payment
    assert substrate.submissions == [] and _record() is None


def _record():
    path = gamma_module.UNRESOLVED / "5Cold.json"
    return json.loads(path.read_text()) if path.exists() else None


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
def test_the_payment_is_on_disk_before_it_is_broadcast_and_dropped_once_the_chain_answers(monkeypatch, chain):
    substrate = chain()
    at_broadcast = []
    if chain is FakeSubstrate:
        substrate.submit = lambda extrinsic: (at_broadcast.append(_record()), substrate.submissions.append(extrinsic),
                                              types.SimpleNamespace(is_success=True, error_message=None))[-1]
    else:
        original = substrate.submit_signature
        substrate.submit_signature = lambda *a, **k: (at_broadcast.append(_record()), original(*a, **k))[-1]

    result, _, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code == 0, result.output
    # a process killed during the broadcast leaves this record, and the next run waits for it to settle
    assert [{k: v for k, v in r.items() if k != "hash"} for r in at_broadcast] == [
        {"nonce": 7, "born": 100, "dies": 100 + gamma_module.ERA_PERIOD}
    ]
    assert at_broadcast[0]["hash"].startswith("0x") and len(at_broadcast[0]["hash"]) == 66
    assert _record() is None


@pytest.mark.parametrize("content", ["", "{\"nonce\": 7", "[]"], ids=["empty", "truncated", "wrong-shape"])
def test_an_unreadable_record_refuses_before_anything_is_signed(monkeypatch, content):
    gamma_module.UNRESOLVED.mkdir(parents=True)
    (gamma_module.UNRESOLVED / "5Cold.json").write_text(content)
    substrate = FakeSubstrate()

    result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code != 0 and "Cannot read" in result.output, result.output
    assert substrate.submissions == [] and "unlock" not in events


def test_a_bittensor_11_without_the_error_map_refuses_before_anything_is_signed(monkeypatch):
    substrate = FakeV11Subtensor()

    result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, error_map=None)

    assert result.exit_code != 0 and "Nothing was signed" in result.output, result.output
    assert substrate.submissions == [] and "unlock" not in events and _record() is None


G = 10**9


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
@pytest.mark.parametrize(
    "cap, bucket, refused, says",
    [
        (10 * G, (10 * G, 100), True, "Waiting does not help"),  # 3 held + ~24.5 minted is above the whole cap
        (40 * G, (5 * G, 100), True, "wait about"),  # under the cap, but only 5 gamma has refilled
        (40 * G, (5 * G, 100 - 7200), False, None),  # a day of blocks since: the bucket is full again
    ],
    ids=["above-the-cap", "not-refilled", "refilled"],
)
def test_a_capped_deposit_holding_gamma_refuses_a_payment_the_cap_cannot_spend_before_signing(
    monkeypatch, chain, cap, bucket, refused, says
):
    substrate = chain(spec_493=True, held=[3 * G, 0])
    substrate.cap = {"daily_spend_cap": {"cap": cap, "bucket": bucket}}

    result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    if not refused:
        assert result.exit_code == 0, result.output
        return
    envelope = json.loads(result.output.strip().splitlines()[-1])
    assert result.exit_code != 0 and envelope["error"]["code"] == "gamma_spend_cap", result.output
    assert says in json.dumps(envelope) and "set_gamma_deposit_daily_spend_cap" in json.dumps(envelope)
    # the exact minimum, never rounded below it; raising the cap adds nothing to what can be spent at once
    expected = gamma_module.tao_rao_for_usd(25, GAMMA_PER_TAO) * GAMMA_PER_TAO // 10**9
    need = 3 * G + gamma_module.min_gamma_out(expected)
    assert f"at least {gamma_module._gamma(need)} gamma" in envelope["error"]["hint"]
    assert "Remove the cap" in envelope["error"]["hint"] and "wait for it to refill" in envelope["error"]["hint"]
    # nothing unlocked, signed or sent: the batch would have been refused and the mint reverted
    assert events == [] and substrate.submissions == [] and substrate.nonces == []


def _watch_finalized(substrate, *unwatch_replies):
    """author_submitAndWatchExtrinsic on bittensor 10's locked driver (async-substrate-interface 2.0.3
    ``submit_extrinsic``): the watch is answered with ``finalized``, then the driver's result handler makes the
    author_unwatchExtrinsic cleanup as its own ``rpc_request``, which resends after a reply timeout until
    ``max_retries`` sends and then raises MaxRetriesExceeded out of the watch."""

    def rpc_request(method, params):
        assert method == "author_unwatchExtrinsic"
        for attempt in range(1, substrate.max_retries + 1):
            if unwatch_replies[min(attempt, len(unwatch_replies)) - 1] != "timeout":
                return {"result": True}
        raise MaxRetriesExceeded("Max retries reached.")

    def submit(extrinsic):
        substrate.watch_send_count += 1
        substrate.rpc_request("author_unwatchExtrinsic", ["0xsub"])  # inside the watch, after "finalized"
        substrate.submissions.append(extrinsic)
        return types.SimpleNamespace(is_success=True, error_message=None)

    substrate.rpc_request = rpc_request
    return submit


@pytest.mark.parametrize("unwatch_replies", [("timeout", "ok"), ("timeout",)], ids=["retried", "lost"])
def test_a_finalized_payment_stands_when_the_unwatch_cleanup_reply_is_lost(monkeypatch, unwatch_replies):
    substrate = FakeSubstrate()
    substrate.max_retries, substrate.watch_send_count = 5, 0
    substrate.submit = _watch_finalized(substrate, *unwatch_replies)
    driver_rpc = substrate.rpc_request

    result, _, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code == 0 and "transfer_failed" not in result.output, result.output
    assert _record() is None and substrate.watch_send_count == 1
    assert substrate.max_retries == 5 and substrate.rpc_request is driver_rpc


def test_a_run_waits_for_another_paying_from_the_same_coldkey_and_keeps_its_record(monkeypatch):
    # one run checked nonce 7 and paid; it then sent nonce 8 and lost the reply, and still holds the coldkey's lock
    gamma_module.UNRESOLVED.mkdir(parents=True)
    pending = {"nonce": 8, "dies": 300}
    (gamma_module.UNRESOLVED / "5Cold.json").write_text(json.dumps(pending))
    fd = os.open(gamma_module.UNRESOLVED / "5Cold.lock", os.O_RDWR | os.O_CREAT, 0o600)
    assert try_lock(fd)
    substrate = FakeSubstrate()
    substrate.final_nonce = 7  # a stale view: the paused run checked before nonce 7 finalized
    try:
        result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)
    finally:
        os.close(fd)

    assert result.exit_code != 0 and "Another 'lium fund --gamma'" in result.output, result.output
    assert _record() == pending and substrate.submissions == [] and events == []


ARGS = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]
CHAINS = pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])


def _lose_answers(substrate, lost=True):
    """The chain takes the payment but its answer never arrives (lost=False: answers arrive again)."""
    if isinstance(substrate, FakeSubstrate):
        substrate.submit = _lost_reply if lost else None
    elif lost:
        def unknown(unsigned, signature, **kwargs):
            substrate.nonces.append(unsigned.nonce)
            return types.SimpleNamespace(success=False, message="disconnected", data={"submission_outcome_unknown": True})

        substrate.submit_signature = unknown
    else:
        del substrate.submit_signature


def _finalize(substrate, number):
    setattr(substrate, "finalized_number" if isinstance(substrate, FakeSubstrate) else "finalized_block", number)


@CHAINS
@pytest.mark.parametrize("outcome", ["ExtrinsicSuccess", "ExtrinsicFailed"], ids=["finalized-success", "finalized-failure"])
def test_a_payment_whose_answer_was_lost_is_resolved_by_its_hash_before_another_is_signed(monkeypatch, chain, outcome):
    substrate = chain()
    _lose_answers(substrate)
    result, _, _ = _run(monkeypatch, ARGS, substrate)
    assert result.exit_code != 0 and _record()["nonce"] == 7

    # it was included and finalized (its nonce is used) while Lium had not credited it yet
    _lose_answers(substrate, lost=False)
    substrate.final_nonce, hash_ = 8, _record()["hash"]
    _finalize(substrate, 110)
    substrate.blocks = {104: [("0x" + "ab" * 32, "ExtrinsicSuccess"), (hash_, outcome)]}
    result, events, _ = _run(monkeypatch, ARGS, substrate)

    assert _record() is None
    if outcome == "ExtrinsicSuccess":  # reported, and nothing new signed
        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code != 0 and envelope["error"]["code"] == "gamma_payment_landed", result.output
        assert "block 104" in json.dumps(envelope) and substrate.nonces == [7] and "unlock" not in events
        result, _, _ = _run(monkeypatch, ARGS, substrate)  # asked again: a new payment
    else:
        assert "failed in block 104" in result.output, result.output
    assert result.exit_code == 0, result.output
    assert substrate.nonces == [7, 8]


@CHAINS
def test_a_payment_that_expired_without_inclusion_is_dropped_and_the_next_one_signs(monkeypatch, chain):
    substrate = chain()
    _lose_answers(substrate)
    _run(monkeypatch, ARGS, substrate)

    _lose_answers(substrate, lost=False)
    _finalize(substrate, 100 + gamma_module.ERA_PERIOD + 1)  # past its era, its nonce never used
    result, _, _ = _run(monkeypatch, ARGS, substrate)

    assert result.exit_code == 0, result.output
    assert "never reached the chain" in result.output and substrate.nonces == [7, 7] and _record() is None


@CHAINS
def test_a_used_nonce_alone_never_drops_the_record(monkeypatch, chain):
    substrate = chain()
    _lose_answers(substrate)
    _run(monkeypatch, ARGS, substrate)
    recorded = _record()

    # included, but its block's result cannot be read (yet)
    _lose_answers(substrate, lost=False)
    substrate.final_nonce = 8
    substrate.blocks = {100: [(recorded["hash"], None)]}
    result, events, _ = _run(monkeypatch, ARGS, substrate)

    assert result.exit_code != 0 and "could not be read" in result.output, result.output
    assert _record() == recorded and substrate.nonces == [7] and "unlock" not in events


@CHAINS
@pytest.mark.parametrize(
    "final_nonce, finalized, kept",
    [(8, 110, True), (7, 100 + gamma_module.ERA_PERIOD + 1, False)],
    ids=["nonce-used", "expired-nonce-unused"],
)
def test_an_older_record_without_a_hash_is_kept_once_its_nonce_is_used(monkeypatch, chain, final_nonce, finalized, kept):
    # the released CLI recorded only the nonce and the era's end: which transaction used the nonce cannot be read
    legacy = {"nonce": 7, "dies": 100 + gamma_module.ERA_PERIOD}
    gamma_module.UNRESOLVED.mkdir(parents=True)
    (gamma_module.UNRESOLVED / "5Cold.json").write_text(json.dumps(legacy))
    substrate = chain()
    substrate.final_nonce = final_nonce
    _finalize(substrate, finalized)

    result, events, _ = _run(monkeypatch, ARGS, substrate)

    if kept:
        assert result.exit_code != 0 and "delete the file to pay again" in result.output, result.output
        assert _record() == legacy and substrate.nonces == [] and "unlock" not in events
    else:  # past its era with its nonce never used: it can no longer land
        assert result.exit_code == 0, result.output
        assert _record() is None and substrate.nonces == [7]


@pytest.mark.parametrize("outcome", ["ExtrinsicSuccess", "ExtrinsicFailed"], ids=["paid", "failed"])
def test_a_bittensor_11_result_in_a_block_without_readable_events_keeps_the_record(monkeypatch, outcome):
    # bittensor 11.3.0 answers success=False with the block when it found the payment but read no dispatch events
    substrate = FakeV11Subtensor()

    def unreadable(unsigned, signature, **kwargs):
        substrate.nonces.append(unsigned.nonce)
        return types.SimpleNamespace(success=False, message="", data={}, block_hash="0xfinal104",
                                     error=types.SimpleNamespace(name=None), events=[])

    substrate.submit_signature = unreadable
    result, _, _ = _run(monkeypatch, ARGS, substrate)
    assert result.exit_code != 0 and "Do not re-send yet" in result.output, result.output
    recorded = _record()
    assert recorded["nonce"] == 7

    # its events are readable once the next run looks: they decide, before anything new is signed
    del substrate.submit_signature
    substrate.final_nonce = 8
    _finalize(substrate, 110)
    substrate.blocks = {104: [(recorded["hash"], outcome)]}
    result, _, _ = _run(monkeypatch, ARGS, substrate)

    assert _record() is None
    if outcome == "ExtrinsicSuccess":
        assert result.exit_code != 0 and "gamma_payment_landed" in result.output, result.output
        assert substrate.nonces == [7]
    else:
        assert result.exit_code == 0 and "failed in block 104" in result.output, result.output
        assert substrate.nonces == [7, 8]


class _TimedOutReceipt:
    @property
    def is_success(self):
        raise TimeoutError("receipt read timed out")


def _receipt_without_events():
    """async-substrate-interface 2.0.3's own ExtrinsicReceipt for a finalized payment whose block's events read
    empty: ``is_success`` and ``error_message`` are both None."""
    from async_substrate_interface.sync_substrate import ExtrinsicReceipt

    substrate = types.SimpleNamespace(get_events=lambda block_hash: [],
                                      get_block=lambda block_hash: {"extrinsics": [types.SimpleNamespace(
                                          extrinsic_hash=bytes([7]) * 32)]})
    receipt = ExtrinsicReceipt(substrate=substrate, extrinsic_hash="0x" + "07" * 32, block_hash="0xblock104")
    assert receipt.is_success is None and receipt.error_message is None
    return receipt


@pytest.mark.parametrize("receipt", [_TimedOutReceipt, _receipt_without_events], ids=["read-timed-out", "events-empty"])
def test_a_lazy_receipt_whose_result_read_fails_keeps_the_record(monkeypatch, receipt):
    substrate = FakeSubstrate(submit=lambda extrinsic: receipt())

    result, _, _ = _run(monkeypatch, ARGS, substrate)

    assert result.exit_code != 0 and "Do not re-send yet" in result.output
    assert _record()["nonce"] == 7


@CHAINS
@pytest.mark.parametrize("capped", [False, True], ids=["uncapped", "cap-used-too"])
def test_a_proxy_spend_while_linking_sizes_and_checks_the_payment_from_the_deposit_after_linking(
    monkeypatch, chain, capped
):
    substrate = chain(spec_493=True, held=[3 * G, 0])
    if capped:
        substrate.cap = {"daily_spend_cap": {"cap": 40 * G, "bucket": (40 * G, 100)}}

    def proxy_spends():  # an Any proxy spends the deposit (and the cap's bucket) with its own nonce
        substrate.held[0] = 0
        if capped:
            substrate.cap = {"daily_spend_cap": {"cap": 40 * G, "bucket": (5 * G, 100)}}

    result, _, _ = _run(monkeypatch, ARGS, substrate, on_link=proxy_spends)

    if capped:
        envelope = json.loads(result.output.strip().splitlines()[-1])
        assert result.exit_code != 0 and envelope["error"]["code"] == "gamma_spend_cap", result.output
        assert substrate.submissions == [] and substrate.nonces == []
        return
    assert result.exit_code == 0, result.output
    (_, _, params), _ = substrate.submitted
    calls = params["calls"]
    mint, spend = (c[1][2] for c in calls) if chain is FakeV11Subtensor else (c[2] for c in calls)
    assert spend["amount"] == mint["min_amount_out"]  # nothing left in the deposit to add
