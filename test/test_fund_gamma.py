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

    pending = False  # a transaction from the coldkey still in the pool

    def get_account_next_index(self, address):
        assert address == "5Cold"
        return 8 if self.pending else 7

    def get_metadata_call_function(self, module, function):
        # the shape finney's metadata returns: a Variant whose value lists the call's fields
        assert (module, function) == ("SubtensorModule", "mint_gamma")
        names = ["hotkey", "netuid", "amount", "min_amount_out"] + ([] if self.spec_493 else ["spender"])
        return types.SimpleNamespace(value={"name": "mint_gamma", "fields": [{"name": n} for n in names + ["daily_spend_cap"]]})

    def query(self, module, storage, params):
        if (module, storage) == ("System", "Account"):
            return types.SimpleNamespace(value={"nonce": 7})
        assert (module, storage, params) == ("SubtensorModule", "GammaDeposits", [51, "5Cold"])
        held = self.held.pop(0)
        return types.SimpleNamespace(value=None if held is None else {"coldkey": "5Cold", "balance": held})

    def runtime_call(self, api, method, params):
        assert (api, method, params[0]) == ("GammaRuntimeApi", "sim_mint_gamma", 51)
        self.calls.append(params[1])
        return types.SimpleNamespace(value={"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)})

    def compose_call(self, call_module, call_function, call_params):
        return (call_module, call_function, call_params)

    def create_signed_extrinsic(self, call, keypair):
        return (call, keypair)

    def submit_extrinsic(self, extrinsic, wait_for_inclusion, wait_for_finalization):
        assert wait_for_inclusion and wait_for_finalization
        if self.submit:
            return self.submit(extrinsic)
        self.submissions.append(extrinsic)
        self.submitted = self.submitted or extrinsic
        return types.SimpleNamespace(is_success=self.success, error_message=None if self.success else "Slippage")

    def subtensor(self):
        return types.SimpleNamespace(substrate=self)


class FakeV11Subtensor:
    """bittensor 11: no ``.substrate``; ``runtime`` reads and ``submit_call`` sends a ``bt.calls`` builder."""

    def __init__(self, success=True, held=None):
        self.success, self.submitted, self.held, self.submissions = success, None, list(held or [None, None]), []

    pending = False

    def prepare_call(self, call, address):
        assert call == ("Utility", "batch_all", {"calls": []}) and address == "5Cold"
        return types.SimpleNamespace(nonce=8 if self.pending else 7)

    def query(self, item, params):
        if item == ("System", "Account"):
            return {"nonce": 7}
        assert (item, params) == (("SubtensorModule", "GammaDeposits"), [51, "5Cold"])
        held = self.held.pop(0)
        return None if held is None else {"coldkey": "5Cold", "balance": held}

    def runtime(self, method, params):
        assert method == ("GammaRuntimeApi", "sim_mint_gamma") and params[0] == 51
        return {"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)}

    def submit_call(self, call, signer, wait_for_inclusion, wait_for_finalization):
        # the unlocked keypair itself: given a wallet, bittensor 11 unlocks it again and prompts under the spinner
        assert wait_for_inclusion and wait_for_finalization and not hasattr(signer, "unlock_coldkey")
        module, function, params = call
        self.submissions.append(((module, function, params), signer))
        self.submitted = self.submitted or self.submissions[-1]
        return types.SimpleNamespace(success=self.success, message="" if self.success else "Slippage")

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


@pytest.mark.parametrize("chain", [FakeSubstrate, FakeV11Subtensor], ids=["bittensor<=10", "bittensor11"])
def test_gamma_links_the_coldkey_and_mints_naming_the_spender(monkeypatch, chain):
    substrate = chain()

    result, events, coldkey = _run(monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate)

    assert result.exit_code == 0, result.output
    assert events == ["unlock", ("sign", "challenge"), ("link", "5Cold", "0x0102")]
    (module, function, params), keypair = substrate.submitted
    rao = gamma_module.tao_rao_for_usd(25, GAMMA_PER_TAO)
    expected = rao * GAMMA_PER_TAO // 10**9
    assert (module, function, keypair) == ("SubtensorModule", "mint_gamma", coldkey)
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


@pytest.mark.parametrize("held, spent_extra, rest", HELD)
def test_bittensor_11_built_for_runtime_493_batches_mint_and_spend(monkeypatch, held, spent_extra, rest):
    substrate = FakeV11Subtensor(held=held)

    result, _, coldkey = _run(
        monkeypatch, ["fund", "--gamma", "-w", "w", "-a", "25", "-y", "--json"], substrate, calls=_calls(spec_493=True)
    )

    assert result.exit_code == 0, result.output
    (module, function, params), keypair = substrate.submitted
    assert (module, function, keypair) == ("Utility", "batch_all", coldkey)
    (_, _, mint), (_, _, spend) = params["calls"]
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

    assert result.exit_code != 0 and "still pending" in result.output
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
