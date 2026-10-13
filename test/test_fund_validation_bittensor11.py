"""`lium fund` address checks on bittensor 11, which removed ``bt.utils`` (DAH-4235)."""

import types

import pytest

from lium.cli.fund.validation import resolve_hotkey, validate_ss58

COLDKEY = "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY"
HOTKEY = "5FHneW46xGXgs5mUiveU4sbTyGBzmstUspZC92UhjJM694ty"


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
        (validate_ss58, (COLDKEY,), (COLDKEY, "")),
        (validate_ss58, ("not-an-address",), (None, "invalid SS58 address: not-an-address")),
        (resolve_hotkey, (HOTKEY, "default"), (HOTKEY, "")),
    ],
)
def test_address_checks_work_on_bittensor_11(check, args, expected):
    assert check(*args, _Bittensor11({COLDKEY, HOTKEY})) == expected


def test_address_checks_still_use_bt_utils_on_older_bittensor():
    older = types.SimpleNamespace(utils=types.SimpleNamespace(is_valid_ss58_address=lambda value: value == COLDKEY))

    assert validate_ss58(COLDKEY, older) == (COLDKEY, "")
    assert validate_ss58("not-an-address", older)[0] is None
