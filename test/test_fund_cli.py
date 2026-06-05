"""DAH-2154: NowPayments crypto funding is retired; TAO funding is kept.

The `lium fund` command continues to support the on-chain TAO wallet transfer
flow. The `lium fund crypto` NowPayments subgroup and its SDK helpers have been
removed.
"""

import json
import sys
import types
from decimal import Decimal

import pytest
from click.testing import CliRunner

import lium.sdk as sdk
from lium.cli.actions import ActionResult
from lium.cli import balance as balance_module
from lium.cli.cli import cli
from lium.cli.fund import command as fund_module
from lium.sdk import AlphaQuote, Config, Lium
from lium.sdk.exceptions import LiumNotFoundError, LiumServerError


def test_fund_help_documents_tao_flow():
    result = CliRunner().invoke(cli, ["fund", "--help"])

    assert result.exit_code == 0
    assert "TAO" in result.output
    assert "lium fund -w default -a 1.5" in result.output
    # The retired NowPayments crypto flow must not be advertised anymore.
    assert "crypto" not in result.output.lower()


def test_fund_tao_dispatch_runs(monkeypatch):
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace())

    class FakeLium:
        def balance(self):
            return 10.0

    class FakeLoadWalletAction:
        def execute(self, ctx):
            return ActionResult(
                ok=True,
                data={"wallet": object(), "address": "coldkey"},
            )

    class FakeCheckWalletRegistrationAction:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"registered": True})

    class FakeExecuteTransferAction:
        def execute(self, ctx):
            assert ctx["tao_amount"] == 1.5
            return ActionResult(ok=True, data={})

    monkeypatch.setattr(fund_module, "Lium", FakeLium)
    monkeypatch.setattr(fund_module, "LoadWalletAction", FakeLoadWalletAction)
    monkeypatch.setattr(
        fund_module, "CheckWalletRegistrationAction", FakeCheckWalletRegistrationAction
    )
    monkeypatch.setattr(fund_module, "ExecuteTransferAction", FakeExecuteTransferAction)

    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5", "-y"])

    assert result.exit_code == 0
    assert "Done." in result.output


def test_fund_crypto_subcommand_is_retired():
    # The old NowPayments flow was `lium fund crypto invoice ...`; it must no
    # longer be a recognised command/option.
    result = CliRunner().invoke(
        cli,
        ["fund", "crypto", "invoice", "--amount-usd", "25", "--currency", "usdttrc20"],
    )

    assert result.exit_code != 0


def test_balance_json(monkeypatch):
    class FakeLium:
        def balance(self):
            return 42.5

    monkeypatch.setattr(balance_module, "Lium", FakeLium)

    result = CliRunner().invoke(cli, ["balance", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"balance_usd": 42.5}


def test_sdk_no_longer_exposes_nowpayments():
    assert not hasattr(sdk, "NowPaymentsCurrency")
    assert not hasattr(sdk, "NowPaymentsInvoice")
    assert not hasattr(Lium, "nowpayments_currencies")
    assert not hasattr(Lium, "create_nowpayments_invoice")


# ---------------------------------------------------------------------------
# Alpha-token top-up (`lium fund --alpha`)
# ---------------------------------------------------------------------------

from lium.cli.fund import actions as fund_actions  # noqa: E402

HK = "5HotKeyExampleSS58AddressForTestingAAAAAAAAAAAAAAAAA"

# Distinct from the TAO-path constant so tests prove the alpha destination is
# sourced from the pay API (/wallet/company/) and not the hardcoded constant.
FAKE_WALLET_HASH = "5CompanyWalletHashFromPayApiBBBBBBBBBBBBBBBBBBBBBBBBB"


class FakeBalance:
    """Minimal alpha-denominated Balance: supports from_tao/set_unit/+/-/</.tao."""

    def __init__(self, amount, netuid=0):
        self.amount = float(amount)
        self.netuid = netuid

    @classmethod
    def from_tao(cls, amount, netuid=0):
        return cls(amount, netuid)

    @classmethod
    def from_rao(cls, rao, netuid=0):
        # 1 alpha = 1e9 rao (same scale as TAO).
        return cls(float(rao) / 1e9, netuid)

    def set_unit(self, netuid):
        self.netuid = netuid
        return self

    @property
    def tao(self):
        return self.amount

    def _same_unit(self, other):
        # Mirror real bittensor Balance: arithmetic across different netuids (e.g.
        # alpha vs TAO) is forbidden. Guards against re-introducing amount+fee bugs.
        if self.netuid != other.netuid:
            raise TypeError(
                "Cannot perform any operations between balances of different "
                f"currencies: netuid {self.netuid} vs {other.netuid}"
            )

    def __add__(self, other):
        self._same_unit(other)
        return FakeBalance(self.amount + other.amount, self.netuid)

    def __sub__(self, other):
        self._same_unit(other)
        return FakeBalance(self.amount - other.amount, self.netuid)

    def __lt__(self, other):
        self._same_unit(other)
        return self.amount < other.amount

    def __repr__(self):
        return f"{self.amount}alpha(netuid={self.netuid})"


class FakeStakeInfo:
    def __init__(self, hotkey_ss58, netuid, stake, locked, coldkey_ss58="coldkey"):
        self.hotkey_ss58 = hotkey_ss58
        self.coldkey_ss58 = coldkey_ss58
        self.netuid = netuid
        self.stake = stake
        self.locked = locked


def _stake(hotkey=HK, netuid=51, stake=5.0, locked=0.0):
    # A real StakeInfo's stake/locked Balances live on the stake's OWN subnet, so the
    # FakeBalance unit must track ``netuid`` (not a hardcoded 51) — otherwise a
    # non-51 netuid would trip the cross-currency guard against the (netuid-correct)
    # transfer amount.
    return FakeStakeInfo(
        hotkey, netuid, FakeBalance(stake, netuid), FakeBalance(locked, netuid)
    )


class FakeSubtensor:
    """Records transfer_stake; serves stake-info reads (per-call, last repeats)."""

    def __init__(self, stake_reads, fee=0.01, fee_raises=False, transfer_result=True):
        # stake_reads: list of "reads", each a list[FakeStakeInfo].
        self.stake_reads = stake_reads
        self._call = 0
        # Real stake-movement fee is TAO-denominated (netuid 0), distinct from the
        # alpha amount (netuid 51) — so combining them must raise, never silently add.
        self.fee = None if fee is None else FakeBalance(fee, 0)
        self.fee_raises = fee_raises
        self.transfer_result = transfer_result
        self.transfer_called = False
        self.transfer_kwargs = None

    def get_stake_info_for_coldkey(self, coldkey, block=None):
        idx = min(self._call, len(self.stake_reads) - 1)
        self._call += 1
        return self.stake_reads[idx]

    def get_stake_movement_fee(self, **kwargs):
        if self.fee_raises:
            raise RuntimeError("fee API unavailable")
        return self.fee

    def transfer_stake(self, **kwargs):
        self.transfer_called = True
        self.transfer_kwargs = kwargs
        return self.transfer_result


class _RaisingKey:
    """A hotkey whose ``.ss58_address`` raises — simulates a missing keyfile."""

    def __init__(self, name):
        self._name = name

    @property
    def ss58_address(self):
        raise FileNotFoundError(f"no hotkey '{self._name}'")


def _make_bt(subtensor, valid_ss58=True, hotkey_names=None):
    bt = types.SimpleNamespace()
    bt.Balance = FakeBalance
    # Real bittensor >=8 exposes ``bt.Subtensor``; mirror that (lowercase alias kept
    # so the version-safe ``subtensor_class`` lookup is exercised either way).
    bt.Subtensor = lambda: subtensor
    bt.subtensor = lambda: subtensor
    # hotkey_names maps a hotkey NAME -> its public ss58 (for the name-resolution
    # path). bt.wallet accepts an optional hotkey kwarg, mirroring real bittensor:
    # a known name yields readable hotkey/hotkeypub ss58; an unknown name raises on
    # attribute access (as a missing keyfile would).
    names = hotkey_names or {}

    def _wallet(name=None, hotkey=None, path=None):
        ns = types.SimpleNamespace(
            coldkeypub=types.SimpleNamespace(ss58_address="coldkey")
        )
        if hotkey is not None:
            ss58 = names.get(hotkey)
            if ss58 is None:
                ns.hotkey = _RaisingKey(hotkey)
                ns.hotkeypub = _RaisingKey(hotkey)
            else:
                ns.hotkey = types.SimpleNamespace(ss58_address=ss58)
                ns.hotkeypub = types.SimpleNamespace(ss58_address=ss58)
        return ns

    # Real bittensor >=8 exposes the class as ``bt.Wallet``; mirror that so the
    # tests exercise the version-safe ``wallet_class`` lookup. (``bt.wallet`` kept
    # as a lowercase alias for robustness.)
    bt.Wallet = _wallet
    bt.wallet = _wallet
    # valid_ss58 may be a bool (applies to every address) or a callable for
    # per-address control (e.g. accept the hotkey but reject a bad funding hash).
    is_valid = valid_ss58 if callable(valid_ss58) else (lambda a: valid_ss58)
    bt.utils = types.SimpleNamespace(is_valid_ss58_address=is_valid)
    return bt


def _patch_common(
    monkeypatch,
    subtensor,
    valid_ss58=True,
    *,
    wallet_hash=FAKE_WALLET_HASH,
    convert_netuids=(51,),
    alpha_per_usd=1.0,
    rate=1.0,
    alpha_amount_override=None,
    convert_error=None,
    company_error=None,
    hotkey_names=None,
):
    """Patch bittensor + the SDK/registration seams; keep the real alpha actions.

    The fake ``Lium`` mirrors the new pay-API surface:
      - ``convert_alpha(usd)`` -> ``AlphaQuote`` (USD->alpha; netuid served per call,
        last value repeating, so Phase-B drift can be exercised).
      - ``company_wallet(app_id)`` -> the funding ``wallet_hash``.
      - ``_discover_app_id`` -> a fixed app id.
    """
    monkeypatch.setitem(
        sys.modules, "bittensor", _make_bt(subtensor, valid_ss58, hotkey_names)
    )
    netuids = list(convert_netuids)

    class FakeLium:
        def __init__(self):
            self._convert_calls = 0

        def _discover_app_id(self, bt_wallet=None):
            return "app-123"

        def company_wallet(self, app_id):
            if company_error is not None:
                raise company_error
            return wallet_hash

        def convert_alpha(self, usd):
            if convert_error is not None:
                raise convert_error
            idx = self._convert_calls
            self._convert_calls += 1
            usd_d = Decimal(str(usd))
            if alpha_amount_override is None:
                alpha = usd_d * Decimal(str(alpha_per_usd))
            elif isinstance(alpha_amount_override, (list, tuple)):
                # Per-call alpha (last repeats) so Phase-B amount-pinning is testable.
                a = alpha_amount_override[min(idx, len(alpha_amount_override) - 1)]
                alpha = Decimal(str(a))
            else:
                alpha = Decimal(str(alpha_amount_override))
            return AlphaQuote(
                usd=usd_d,
                alpha_amount=alpha,
                rate=Decimal(str(rate)),
                netuid=netuids[min(idx, len(netuids) - 1)],
            )

    class FakeReg:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"registered": True})

    monkeypatch.setattr(fund_module, "Lium", FakeLium)
    monkeypatch.setattr(fund_module, "CheckWalletRegistrationAction", FakeReg)


def test_alpha_happy_path(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert "Done." in result.output
    assert sub.transfer_called
    kw = sub.transfer_kwargs
    # Destination comes from /wallet/company/ (the fetched wallet_hash), NOT the
    # TAO-path constant.
    assert kw["destination_coldkey_ss58"] == FAKE_WALLET_HASH
    assert kw["hotkey_ss58"] == HK
    assert kw["origin_netuid"] == 51 and kw["destination_netuid"] == 51
    assert kw["amount"].tao == 2.0 and kw["amount"].netuid == 51


def test_alpha_insufficient_free_alpha_aborts(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=1.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    # Human-mode aborts print the error and exit 0 (same as the TAO path);
    # the binding guarantee is that no transfer was signed.
    assert "Insufficient free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_fee_not_added_to_alpha_gate(monkeypatch):
    # The TAO-denominated movement fee must NOT be folded into the alpha gate:
    # free == requested alpha still transfers even with a non-zero fee present.
    sub = FakeSubtensor([[_stake(stake=2.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called


def test_alpha_fee_unavailable_still_transfers(monkeypatch):
    # Fee is advisory: when the node can't report it we proceed on free >= amount
    # (no hard abort, no buffer). free=5 >= amount=2 -> transfer goes through.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=None, fee_raises=True)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called


def test_alpha_fee_unavailable_still_transfers_json(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=None, fee_raises=True)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y", "--json"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["tx"]["fee_modeled"] is False  # fee couldn't be modeled


def test_alpha_fee_unavailable_still_gates_on_free(monkeypatch):
    # Even without a fee, the bare free >= amount gate still protects us.
    sub = FakeSubtensor([[_stake(stake=1.0)]], fee=None, fee_raises=True)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "Insufficient free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_fee_buffer_option_removed(monkeypatch):
    # --fee-buffer was deleted; passing it must be a usage error, not a silent no-op.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli,
        ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "--fee-buffer", "0.1", "-y"],
    )

    assert result.exit_code != 0
    assert "no such option" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_transfer_failure_surfaces_message(monkeypatch):
    # bittensor >=8 returns an ExtrinsicResponse(.success/.message) instead of a
    # bool; a failed transfer must surface its message, not be reported as success.
    sub = FakeSubtensor(
        [[_stake(stake=5.0)]],
        fee=0.01,
        transfer_result=types.SimpleNamespace(success=False, message="on-chain rejected"),
    )
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code != 0
    assert "on-chain rejected" in result.output
    assert sub.transfer_called  # we did attempt it, but it failed


def test_alpha_shrink_between_reads(monkeypatch):
    # Phase A read shows ample; Phase B re-read shows shrunk -> abort, no transfer.
    sub = FakeSubtensor([[_stake(stake=5.0)], [_stake(stake=1.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "Insufficient free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_multiple_rows_aborts(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0), _stake(stake=4.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "multiple stake entries" in result.output
    assert not sub.transfer_called


def test_alpha_no_stakeinfo_aborts(monkeypatch):
    sub = FakeSubtensor([[]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "No free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_json_no_yes_requires_confirm(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    called = {"confirm": False}
    monkeypatch.setattr(fund_module.ui, "confirm", lambda *a, **k: called.__setitem__("confirm", True) or True)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "--json"]
    )

    assert result.exit_code != 0
    assert "confirmation required" in result.output
    assert not called["confirm"]
    assert not sub.transfer_called


def test_alpha_json_missing_hotkey(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(cli, ["fund", "--alpha", "-a", "2", "-y", "--json"])

    assert result.exit_code != 0
    assert "hotkey" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_missing_hotkey_interactive_prompts(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)
    monkeypatch.setattr(fund_module.Prompt, "ask", staticmethod(lambda *a, **k: HK))

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called


def test_alpha_invalid_hotkey_neither_ss58_nor_name(monkeypatch):
    # "bogus" is neither a valid SS58 nor an existing hotkey name in --wallet, so
    # the resolver hard-fails with a name-aware error before any chain call.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, valid_ss58=False)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", "bogus", "-w", "default", "-a", "2", "-y"]
    )

    assert "neither a valid SS58" in result.output
    assert "wallet 'default'" in result.output
    assert not sub.transfer_called


def test_alpha_hotkey_by_name_resolves(monkeypatch):
    # -k is a wallet hotkey NAME (not an ss58); it resolves to the staked hotkey's
    # public ss58 under --wallet, then the transfer proceeds identically.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch,
        sub,
        valid_ss58=lambda a: a != "myhot",  # the name is not a valid ss58; HK/dest are
        hotkey_names={"myhot": HK},
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", "myhot", "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called
    # The transfer (and stake lookup) use the RESOLVED ss58, never the raw name.
    assert sub.transfer_kwargs["hotkey_ss58"] == HK


def test_alpha_hotkey_by_name_json_echoes_resolved_ss58(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch,
        sub,
        valid_ss58=lambda a: a != "myhot",
        hotkey_names={"myhot": HK},
    )

    result = CliRunner().invoke(
        cli,
        ["fund", "--alpha", "-k", "myhot", "-w", "default", "-a", "2", "-y", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["tx"]["hotkey"] == HK  # resolved ss58, not "myhot"


def test_alpha_hotkey_name_not_found(monkeypatch):
    # A name with no matching keyfile under --wallet -> name-aware hard error,
    # no chain/API call.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch, sub, valid_ss58=lambda a: a != "ghost", hotkey_names={}
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", "ghost", "-w", "default", "-a", "2", "-y"]
    )

    assert "neither a valid SS58" in result.output
    assert not sub.transfer_called


def test_fund_alpha_hotkey_help_mentions_name():
    result = CliRunner().invoke(cli, ["fund", "--help"])

    assert result.exit_code == 0
    # The --hotkey help must advertise that a wallet hotkey name is accepted too.
    assert "wallet hotkey name" in result.output


def test_alpha_displays_free_before_confirm(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)
    # Decline at confirm so we abort right after the display.
    monkeypatch.setattr(fund_module.ui, "confirm", lambda *a, **k: False)

    result = CliRunner().invoke(cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2"])

    assert "Free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_json_success_envelope(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    tx = payload["tx"]
    assert tx["coldkey"] == "coldkey"
    assert tx["hotkey"] == HK
    assert tx["netuid"] == 51
    assert tx["usd"] == 2.0
    assert tx["amount_alpha"] == 2.0
    assert tx["rate"] == 1.0
    assert tx["dest_coldkey"] == FAKE_WALLET_HASH
    assert tx["fee_alpha"] == 0.01
    assert tx["fee_modeled"] is True
    assert tx["fee_caveat"] is None
    assert "on-chain inclusion time" in tx["usd_caveat"]
    assert sub.transfer_called


def test_alpha_locked_alpha_excluded_from_free(monkeypatch):
    # free = stake - locked. With stake=5, locked=3, free=2 < requested 4 -> abort.
    sub = FakeSubtensor([[_stake(stake=5.0, locked=3.0)]], fee=0.0)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "4", "-y"]
    )

    assert "Insufficient free alpha" in result.output
    assert not sub.transfer_called


def test_alpha_amount_rejects_non_finite(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "inf", "-y"]
    )

    assert "positive finite" in result.output
    assert not sub.transfer_called


def test_alpha_decoupled_from_funding_constants():
    # Replaces test_alpha_dest_equals_tao_dest: the alpha path no longer derives its
    # destination from the hardcoded constant (it comes from /wallet/company/), and
    # the hardcoded netuid constant is deleted entirely. The TAO path still uses the
    # shared funding-address constant.
    assert not hasattr(fund_actions.ExecuteAlphaTransferAction, "LIUM_FUNDING_ADDRESS")
    assert not hasattr(fund_actions, "NETUID")
    assert (
        fund_actions.ExecuteTransferAction.LIUM_FUNDING_ADDRESS
        == fund_actions.LIUM_FUNDING_ADDRESS
    )


def test_alpha_messaging_says_alpha_not_tao(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)
    seen = {}
    monkeypatch.setattr(
        fund_module.ui, "confirm", lambda msg, **k: seen.__setitem__("msg", msg) or False
    )

    CliRunner().invoke(cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2"])

    assert "alpha (netuid 51)" in seen["msg"]
    assert "TAO" not in seen["msg"]


def test_alpha_usd_convert_called(monkeypatch):
    # -a is USD; convert maps USD->alpha (0.5 alpha/USD here) and the floored
    # `converted` becomes the transferred amount.
    sub = FakeSubtensor([[_stake(stake=10.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, alpha_per_usd=0.5)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "10", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_called
    assert sub.transfer_kwargs["amount"].tao == 5.0  # 10 USD * 0.5 alpha/USD


def test_alpha_netuid_from_api(monkeypatch):
    # The transfer uses the API-resolved netuid (38 here), not a hardcoded 51.
    sub = FakeSubtensor([[_stake(netuid=38, stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, convert_netuids=(38,))

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    kw = sub.transfer_kwargs
    assert kw["origin_netuid"] == 38 and kw["destination_netuid"] == 38
    assert kw["amount"].netuid == 38


def test_alpha_netuid_mismatch_aborts(monkeypatch):
    # Phase A resolves netuid 51; the Phase-B re-fetch returns 38 -> abort, no transfer.
    sub = FakeSubtensor([[_stake(netuid=51, stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, convert_netuids=(51, 38))

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "netuid changed" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_dest_from_company_wallet(monkeypatch):
    custom = "5DistinctCompanyWalletForThisTestCCCCCCCCCCCCCCCCCCC"
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, wallet_hash=custom)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_kwargs["destination_coldkey_ss58"] == custom


def test_alpha_hard_fail_on_503(monkeypatch):
    # convert_alpha 503 -> abort before any transfer; JSON renders the error envelope.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch, sub, convert_error=LiumServerError("Server error: 503")
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y", "--json"]
    )

    assert result.exit_code != 0
    assert '"ok": false' in result.output
    assert not sub.transfer_called


def test_alpha_hard_fail_on_404(monkeypatch):
    # company_wallet 404 -> abort before any chain call.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch, sub, company_error=LiumNotFoundError("Resource not found")
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "not found" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_rao_floor_never_over_transfer(monkeypatch):
    # A sub-rao quote (2 + 0.9 rao) floors down to exactly 2.0 alpha; we never
    # transfer more than the floor, keeping the free+fee gate exact.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, alpha_amount_override="2.0000000009")

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_kwargs["amount"].tao == 2.0


def test_alpha_usd_caveat_in_prompt_output(monkeypatch):
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub)

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "on-chain inclusion time" in result.output


def test_alpha_shows_destination_before_confirm(monkeypatch):
    # The runtime-resolved destination coldkey must be visible before signing.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, wallet_hash=FAKE_WALLET_HASH)
    seen = {}
    monkeypatch.setattr(
        fund_module.ui, "confirm", lambda msg, **k: seen.__setitem__("msg", msg) or False
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2"]
    )

    assert FAKE_WALLET_HASH in result.output  # shown in the display block
    assert FAKE_WALLET_HASH in seen["msg"]    # and in the confirm prompt
    assert not sub.transfer_called


def test_alpha_invalid_funding_address_aborts(monkeypatch):
    # The hotkey is a valid SS58 but the API returns a bad funding hash -> abort.
    bad = "not-an-ss58"
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(
        monkeypatch, sub, valid_ss58=lambda a: a != bad, wallet_hash=bad
    )

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "invalid funding address" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_self_send_funding_address_aborts(monkeypatch):
    # The API returns the caller's own coldkey as the destination -> abort.
    sub = FakeSubtensor([[_stake(stake=5.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, wallet_hash="coldkey")  # == loaded coldkey address

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert "own coldkey" in result.output.lower()
    assert not sub.transfer_called


def test_alpha_phase_b_amount_pinned_to_confirmed(monkeypatch):
    # The Phase-B re-fetch returns a LARGER alpha (3.0) than Phase A (2.0); the
    # transferred amount must stay the Phase-A floored value (2.0), never the
    # re-fetched value.
    sub = FakeSubtensor([[_stake(stake=10.0)]], fee=0.01)
    _patch_common(monkeypatch, sub, alpha_amount_override=["2.0", "3.0"])

    result = CliRunner().invoke(
        cli, ["fund", "--alpha", "-k", HK, "-w", "default", "-a", "2", "-y"]
    )

    assert result.exit_code == 0, result.output
    assert sub.transfer_kwargs["amount"].tao == 2.0


# ---------------------------------------------------------------------------
# SDK-level tests for the new pay-API methods
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _sdk_lium():
    return Lium(config=Config(api_key="test"))


def test_sdk_convert_alpha_parses_quote(monkeypatch):
    lium = _sdk_lium()
    captured = {}

    def fake_request(method, endpoint, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["kwargs"] = kwargs
        return _Resp({"original": "10", "converted": "12.5", "rate": "0.8", "netuid": 38})

    monkeypatch.setattr(lium, "_request", fake_request)
    quote = lium.convert_alpha("10")

    assert isinstance(quote, AlphaQuote)
    assert quote.netuid == 38
    assert quote.alpha_amount == Decimal("12.5")
    assert quote.rate == Decimal("0.8")
    assert captured["method"] == "GET"
    assert captured["endpoint"] == "/balance/convert/alpha"
    assert captured["kwargs"]["params"] == {"amount": "10"}
    assert captured["kwargs"]["base_url"] == lium.config.base_pay_url


def test_sdk_convert_alpha_503_propagates(monkeypatch):
    lium = _sdk_lium()

    def fake_request(*a, **k):
        raise LiumServerError("Server error: 503")

    monkeypatch.setattr(lium, "_request", fake_request)
    with pytest.raises(LiumServerError):
        lium.convert_alpha("10")


def test_sdk_company_wallet_returns_hash(monkeypatch):
    lium = _sdk_lium()
    captured = {}

    def fake_request(method, endpoint, **kwargs):
        captured["endpoint"] = endpoint
        captured["kwargs"] = kwargs
        return _Resp({"wallet_hash": "5Funding", "id": 1, "created": "now"})

    monkeypatch.setattr(lium, "_request", fake_request)
    assert lium.company_wallet("app-1") == "5Funding"
    assert captured["endpoint"] == "/wallet/company/"
    assert captured["kwargs"]["params"] == {"app_id": "app-1"}
    assert captured["kwargs"]["base_url"] == lium.config.base_pay_url


def test_sdk_company_wallet_404_propagates(monkeypatch):
    lium = _sdk_lium()

    def fake_request(*a, **k):
        raise LiumNotFoundError("Resource not found")

    monkeypatch.setattr(lium, "_request", fake_request)
    with pytest.raises(LiumNotFoundError):
        lium.company_wallet("app-1")


def test_sdk_discover_app_id_single_create_transfer(monkeypatch):
    lium = _sdk_lium()
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append(endpoint)
        assert endpoint == "/tao/create-transfer"
        # Must use the DEFAULT base_url + no pay header (unlike the pay-API calls).
        assert "base_url" not in kwargs
        assert "headers" not in kwargs
        return _Resp({"url": "https://pay/redirect?app_id=APP&customer_id=CUS"})

    monkeypatch.setattr(lium, "_request", fake_request)
    assert lium._discover_app_id() == "APP"
    assert calls.count("/tao/create-transfer") == 1


def test_sdk_add_wallet_returns_app_id_single_create_transfer(monkeypatch):
    lium = _sdk_lium()
    calls = []

    def fake_request(method, endpoint, **kwargs):
        calls.append(endpoint)
        if endpoint == "/token/generate":
            return _Resp({"access_key": "ak"})
        if endpoint == "/tao/create-transfer":
            return _Resp({"url": "https://pay/redirect?app_id=APP&customer_id=CUS"})
        if endpoint == "/token/verify":
            return _Resp({"status": "ok"})
        if endpoint == "/users/me":
            return _Resp({"stripe_customer_id": "CUS"})
        if endpoint.startswith("/wallet/available-wallets"):
            return _Resp([{"wallet_hash": "coldkey"}])
        raise AssertionError(f"unexpected endpoint {endpoint}")

    monkeypatch.setattr(lium, "_request", fake_request)
    bt_wallet = types.SimpleNamespace(
        coldkey=types.SimpleNamespace(
            sign=lambda b: types.SimpleNamespace(hex=lambda: "sig")
        ),
        coldkeypub=types.SimpleNamespace(ss58_address="coldkey"),
    )

    app_id, customer_id = lium.add_wallet(bt_wallet)

    assert app_id == "APP" and customer_id == "CUS"
    assert calls.count("/tao/create-transfer") == 1


def test_fund_no_alpha_uses_tao_path(monkeypatch):
    monkeypatch.setitem(sys.modules, "bittensor", types.SimpleNamespace())
    calls = {"tao": False, "alpha": False}
    monkeypatch.setattr(fund_module, "_legacy_tao_fund", lambda *a, **k: calls.__setitem__("tao", True))
    monkeypatch.setattr(fund_module, "_alpha_fund", lambda *a, **k: calls.__setitem__("alpha", True))

    result = CliRunner().invoke(cli, ["fund", "-w", "default", "-a", "1.5", "-y"])

    assert result.exit_code == 0
    assert calls["tao"] and not calls["alpha"]
