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

    def __init__(self, success=True, submit=None):
        self.success, self.calls, self.submitted, self.submit = success, [], None, submit

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
        self.submitted = extrinsic
        return types.SimpleNamespace(is_success=self.success, error_message=None if self.success else "Slippage")

    def subtensor(self):
        return types.SimpleNamespace(substrate=self)


class FakeV11Subtensor:
    """bittensor 11: no ``.substrate``; ``runtime`` reads and ``submit_call`` sends a ``bt.calls`` builder."""

    def __init__(self, success=True):
        self.success, self.submitted = success, None

    def runtime(self, method, params):
        assert method == ("GammaRuntimeApi", "sim_mint_gamma") and params[0] == 51
        return {"Ok": (1, 1, params[1] * GAMMA_PER_TAO // 10**9)}

    def submit_call(self, call, signer, wait_for_inclusion, wait_for_finalization):
        # the unlocked keypair itself: given a wallet, bittensor 11 unlocks it again and prompts under the spinner
        assert wait_for_inclusion and wait_for_finalization and not hasattr(signer, "unlock_coldkey")
        module, function, params = call
        self.submitted = ((module, function, params), signer)
        return types.SimpleNamespace(success=self.success, message="" if self.success else "Slippage")

    def subtensor(self):
        return self


def _calls():
    mint = lambda **params: ("SubtensorModule", "mint_gamma", params)  # noqa: E731
    return types.SimpleNamespace(SubtensorModule=types.SimpleNamespace(mint_gamma=mint))


def _run(monkeypatch, args, substrate):
    events = []
    coldkey = types.SimpleNamespace(sign=lambda m: (events.append(("sign", m)), b"\x01\x02")[1])
    wallet = types.SimpleNamespace(coldkey=coldkey, unlock_coldkey=lambda: events.append("unlock"))
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace(Subtensor=substrate.subtensor, calls=_calls()))

    class FakeLoadWalletAction:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"wallet": wallet, "address": "5Cold"})

    class FakeLium:
        def link_gamma_hotkey(self, hotkey, sign):
            events.append(("link", hotkey, sign("challenge")))
            return {"hotkey": hotkey, "netuid": 51, "spender": "5Spender"}

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
