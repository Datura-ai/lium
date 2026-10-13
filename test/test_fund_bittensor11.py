"""`lium fund` on bittensor 11 (DAH-4235): ``bt.utils`` is gone, ``Subtensor`` has no ``transfer`` /
``transfer_stake`` / ``get_stake_info_for_coldkey`` (extrinsics are intents run by ``execute``, reads go
through ``read``), and ``Balance`` takes its subnet in ``from_rao`` instead of ``set_unit``."""

import sys
import types

import pytest

from lium.cli.fund.actions import LIUM_FUNDING_ADDRESS, ExecuteAlphaTransferAction, ExecuteTransferAction
from lium.cli.fund.validation import alpha_balance, resolve_hotkey, validate_ss58

# the public SS58 addresses of the Substrate dev accounts //Alice and //Bob
ALICE = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
BOB = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


class _Bittensor11(types.ModuleType):
    """The shape of bittensor 11: ``bt.wallets.is_bittensor_address``, and ``bt.utils`` raising AttributeError."""

    def __init__(self, valid):
        super().__init__("bittensor")
        self.wallets = types.SimpleNamespace(is_bittensor_address=lambda value: value in valid)

    def __getattr__(self, name):
        raise AttributeError(f"bittensor.{name} was removed in v11")


@pytest.mark.parametrize(
    ("check", "args", "expected"),
    [
        (validate_ss58, (ALICE,), (ALICE, "")),
        (validate_ss58, ("not-an-address",), (None, "invalid SS58 address: not-an-address")),
        (resolve_hotkey, (BOB, "default"), (BOB, "")),
    ],
)
def test_address_checks_work_on_bittensor_11(check, args, expected):
    assert check(*args, _Bittensor11({ALICE, BOB})) == expected


def test_address_checks_still_use_bt_utils_on_older_bittensor():
    older = types.SimpleNamespace(utils=types.SimpleNamespace(is_valid_ss58_address=lambda value: value == ALICE))

    assert validate_ss58(ALICE, older) == (ALICE, "")
    assert validate_ss58("not-an-address", older)[0] is None


# ---------------------------------------------------------------------------------------- transfers


class _Balance:
    """bittensor 11's Balance as far as fund uses it: the subnet is given to ``from_rao``; no ``set_unit``."""

    def __init__(self, rao, netuid=0):
        self.rao, self.netuid = int(rao), netuid

    @classmethod
    def from_rao(cls, rao, netuid=0):
        return cls(rao, netuid)

    @classmethod
    def from_tao(cls, tao):
        return cls(round(float(tao) * 1e9))

    def __lt__(self, other):
        assert self.netuid == other.netuid, "bittensor refuses to compare different subnets"
        return self.rao < other.rao

    def __eq__(self, other):
        return (self.rao, self.netuid) == (other.rao, other.netuid)

    def __repr__(self):
        return f"{self.rao}rao@{self.netuid}"


class _Intent:
    def __init__(self, op, **fields):
        self.op, self.fields = op, fields


class _Subtensor11:
    """No legacy methods: reads through ``read``, extrinsics through ``execute`` (records them)."""

    def __init__(self, positions=(), available_rao=0, success=True):
        self.positions, self.available_rao, self.success = list(positions), available_rao, success
        self.executed = []

    def read(self, name, **params):
        if name == "stake_for_coldkey":
            return self.positions
        if name == "stake_availability":
            return {"available": _Balance(self.available_rao, params["netuid"])}
        raise AssertionError(f"unexpected read {name}")

    def execute(self, intent, wallet):
        self.executed.append((intent, wallet))
        return types.SimpleNamespace(success=self.success, message="" if self.success else "Insufficient balance")


def _bt11(subtensor):
    bt = _Bittensor11(set())
    bt.Balance = _Balance
    bt.Subtensor = lambda: subtensor
    bt.intents = types.SimpleNamespace(
        Transfer=lambda **f: _Intent("transfer", **f), TransferStake=lambda **f: _Intent("transfer_stake", **f)
    )
    return bt


def _position(hotkey=BOB, netuid=51, stake_rao=5 * 10**9):
    return types.SimpleNamespace(hotkey=hotkey, coldkey=ALICE, netuid=netuid, stake=_Balance(stake_rao, netuid))


def _alpha_ctx(bt, amount_rao):
    return {
        "bt": bt, "bt_wallet": "wallet", "coldkey_ss58": ALICE, "hotkey_ss58": BOB,
        "amount_bal": _Balance(amount_rao, 51), "netuid": 51, "dest_coldkey": "5Dest",
    }


def test_alpha_transfer_runs_a_transfer_stake_intent_on_bittensor_11():
    subtensor = _Subtensor11([_position(), _position(hotkey="5Other")], available_rao=3 * 10**9)

    result = ExecuteAlphaTransferAction().execute(_alpha_ctx(_bt11(subtensor), 2 * 10**9))

    assert result.ok, result.error
    [(intent, wallet)] = subtensor.executed
    assert (intent.op, wallet) == ("transfer_stake", "wallet")
    assert intent.fields == {
        "dest_coldkey_ss58": "5Dest", "hotkey_ss58": BOB, "origin_netuid": 51, "dest_netuid": 51,
        "amount_alpha": _Balance(2 * 10**9, 51),
    }
    assert result.data["free"] == _Balance(3 * 10**9, 51) and result.data["fee_modeled"] is False


def test_alpha_transfer_counts_the_coldkey_lock_on_bittensor_11():
    """5 alpha staked on the hotkey, 3 available on the subnet (a conviction lock): 4 is refused unsent."""
    subtensor = _Subtensor11([_position()], available_rao=3 * 10**9)

    result = ExecuteAlphaTransferAction().execute(_alpha_ctx(_bt11(subtensor), 4 * 10**9))

    assert not result.ok and "Insufficient free alpha" in result.error
    assert subtensor.executed == []


def test_tao_transfer_runs_a_transfer_intent_on_bittensor_11(monkeypatch):
    subtensor = _Subtensor11()
    monkeypatch.setitem(sys.modules, "bittensor", _bt11(subtensor))

    result = ExecuteTransferAction().execute({"bt_wallet": "wallet", "tao_amount": 0.5})

    assert result.ok, result.error
    [(intent, wallet)] = subtensor.executed
    assert (intent.op, wallet) == ("transfer", "wallet")
    assert intent.fields == {"dest_ss58": LIUM_FUNDING_ADDRESS, "amount_tao": _Balance(5 * 10**8)}


def test_a_refused_tao_transfer_on_bittensor_11_reports_the_chain_message(monkeypatch):
    monkeypatch.setitem(sys.modules, "bittensor", _bt11(_Subtensor11(success=False)))

    result = ExecuteTransferAction().execute({"bt_wallet": "wallet", "tao_amount": 0.5})

    assert not result.ok and result.error == "Insufficient balance"


def test_alpha_balance_tags_the_subnet_on_bittensor_11_and_older():
    assert alpha_balance(types.SimpleNamespace(Balance=_Balance), 7, 51) == _Balance(7, 51)

    class _Older(_Balance):
        def set_unit(self, netuid):
            self.netuid = netuid
            return self

    older = alpha_balance(types.SimpleNamespace(Balance=_Older), 7, 51)
    assert (older.rao, older.netuid) == (7, 51)
