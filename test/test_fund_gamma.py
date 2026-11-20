"""DAH-6385: ``lium fund --gamma`` mints SN51 gamma into a linked deposit Lium credits as USD."""

import json
import sys
import types

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.fund import actions as actions_module
from lium.cli.fund import command as fund_module
from lium.cli.fund import gamma as gamma_module

GAMMA_PER_TAO = 210_561_193_201  # sim_mint_gamma(51, 1 TAO) on finney, 13 Nov 2026


@pytest.fixture(autouse=True)
def _unresolved_file(monkeypatch, tmp_path):
    monkeypatch.setattr(gamma_module, "UNRESOLVED", tmp_path / "unresolved")


def test_usd_is_sized_to_tao_rounded_up_and_floored_two_percent():
    rao = gamma_module.tao_rao_for_usd(25, GAMMA_PER_TAO)

    assert rao * GAMMA_PER_TAO >= 25 * 10**18 > (rao - 1) * GAMMA_PER_TAO
    assert gamma_module.min_gamma_out(25 * 10**9) == 24_500_000_000


class FakeSubstrate:
    """bittensor <= 10: ``Subtensor().substrate`` composes, signs and submits."""

    def __init__(self, success=True, submit=None, spec_493=False, held=None):
        self.success, self.calls, self.submitted, self.submit = success, [], None, submit
        # held: the deposit balance at each read (before the batch, then after it), None for no deposit
        self.spec_493, self.held, self.submissions = spec_493, list(held or [None, None]), []
        self.nonces, self.final_nonce, self.finalized_number = [], 7, 100

    pending = False  # a transaction from the coldkey still in the pool
    cap = {}  # {"daily_spend_cap": {"cap", "bucket": (units, block)}} for a capped deposit

    def get_account_next_index(self, address):
        assert address == "5Cold"
        return self.final_nonce + (1 if self.pending else 0)

    def get_metadata_call_function(self, module, function):
        # the shape finney's metadata returns: a Variant whose value lists the call's fields
        assert (module, function) == ("SubtensorModule", "mint_gamma")
        names = ["hotkey", "netuid", "amount", "min_amount_out"] + ([] if self.spec_493 else ["spender"])
        return types.SimpleNamespace(value={"name": "mint_gamma", "fields": [{"name": n} for n in names + ["daily_spend_cap"]]})

    def query(self, module, storage, params, block_hash=None):
        if (module, storage) == ("System", "Account"):
            assert block_hash == "0xfinal"  # settled means finalized
            return types.SimpleNamespace(value={"nonce": self.final_nonce})
        assert (module, storage, params) == ("SubtensorModule", "GammaDeposits", [51, "5Cold"])
        held = self.held.pop(0)
        return types.SimpleNamespace(value=None if held is None else {"coldkey": "5Cold", "balance": held, **self.cap})

    def runtime_call(self, api, method, params):
        assert (api, method, params[0]) == ("GammaRuntimeApi", "sim_mint_gamma", 51)
        self.calls.append(params[1])
        return types.SimpleNamespace(value={"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)})

    def compose_call(self, call_module, call_function, call_params):
        return (call_module, call_function, call_params)

    def create_signed_extrinsic(self, call, keypair, nonce, era):
        assert era == {"period": gamma_module.ERA_PERIOD, "current": self.finalized_number}
        self.nonces.append(nonce)
        return (call, keypair)

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


class FakeV11Subtensor:
    """bittensor 11: no ``.substrate``; ``runtime`` reads and ``submit_call`` sends a ``bt.calls`` builder."""

    def __init__(self, success=True, held=None, spec_493=False):
        self.success, self.submitted, self.held, self.submissions = success, None, list(held or [None, None]), []
        self.spec_493 = spec_493  # the connected runtime, whatever the installed SDK was generated for
        self.nonces, self.final_nonce, self.finalized_block, self.unknown = [], 7, 100, False

    pending = False
    cap = {}

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
        return types.SimpleNamespace(nonce=nonce, payload=b"payload", call=call)

    def query(self, item, params, block=None):
        if item == ("System", "Account"):
            assert block == self.finalized_block  # settled means finalized
            return {"nonce": self.final_nonce}
        if item == ("SubtensorModule", "GammaSpent"):
            if not self.spec_493:
                raise type("StorageFunctionNotFound", (ValueError,), {})("SubtensorModule.GammaSpent")
            return 0
        assert (item, params) == (("SubtensorModule", "GammaDeposits"), [51, "5Cold"])
        held = self.held.pop(0)
        return None if held is None else {"coldkey": "5Cold", "balance": held, **self.cap}

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
        return types.SimpleNamespace(success=self.success, message="" if self.success else "Slippage", data={})

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


def _run(monkeypatch, args, substrate, calls=None):
    events = []
    coldkey = types.SimpleNamespace(sign=lambda m: (events.append(("sign", m)), b"\x01\x02")[1])
    wallet = types.SimpleNamespace(coldkey=coldkey, unlock_coldkey=lambda: events.append("unlock"))
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace(Subtensor=substrate.subtensor, calls=calls or _calls()))

    class FakeLoadWalletAction:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"wallet": wallet, "address": "5Cold"})

    class FakeLium:
        def link_gamma_hotkey(self, hotkey, sign):
            events.append(("link", hotkey, sign("challenge")))
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


def _pool_answer(code, shape="str"):
    """The node's refusal as bittensor 10's substrate library raises it: async-substrate-interface 2.0.3's
    author_submitAndWatchExtrinsic path raises ``SubstrateRequestException(str(response))`` with the whole answer;
    its other path raises the answer dict itself; older stacks raised the error object alone."""
    error = {"code": code, "message": "Invalid Transaction",
             "data": "Inability to pay some fees (e.g. account balance too low)"}
    answer = {"jsonrpc": "2.0", "id": 1, "error": error}
    raised = {"str": str(answer), "dict": answer, "error": error}[shape]

    def submit(extrinsic):
        raise SubstrateRequestException(raised)
    return submit


@pytest.mark.parametrize("shape", ["str", "dict", "error"])
def test_a_mint_the_node_refuses_clears_its_record_and_the_next_run_proceeds(monkeypatch, shape):
    substrate = FakeSubstrate(submit=_pool_answer(1010, shape))  # bittensor <= 10: the pool refuses, e.g. fees unpaid
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and "1010 Invalid Transaction" in result.output, result.output
    assert _record() is None

    substrate.submit = None  # funded meanwhile: the rerun is not held for a payment that never entered the pool
    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code == 0, result.output
    assert substrate.nonces == [7, 7]


def test_a_mint_the_pool_already_holds_keeps_its_record(monkeypatch):
    substrate = FakeSubstrate(submit=_pool_answer(1013))  # already imported: the same transaction may still land
    args = ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"]

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and _record()["nonce"] == 7

    result, _, _ = _run(monkeypatch, args, substrate)
    assert result.exit_code != 0 and "has not settled yet" in result.output


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
    assert at_broadcast == [{"nonce": 7, "dies": 100 + gamma_module.ERA_PERIOD}]
    assert _record() is None


@pytest.mark.parametrize("content", ["", "{\"nonce\": 7", "[]"], ids=["empty", "truncated", "wrong-shape"])
def test_an_unreadable_record_refuses_before_anything_is_signed(monkeypatch, content):
    gamma_module.UNRESOLVED.mkdir(parents=True)
    (gamma_module.UNRESOLVED / "5Cold.json").write_text(content)
    substrate = FakeSubstrate()

    result, events, _ = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code != 0 and "Cannot read" in result.output, result.output
    assert substrate.submissions == [] and "unlock" not in events


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
    # nothing unlocked, signed or sent: the batch would have been refused and the mint reverted
    assert events == [] and substrate.submissions == [] and substrate.nonces == []
