"""The rent path's 403 detail fills LiumInsufficientBalanceError's required and available amounts."""

import pytest

from lium.sdk.client import permission_error
from lium.sdk.exceptions import LiumInsufficientBalanceError


@pytest.mark.parametrize(
    ("phrase", "required"),
    [("requires at least $0.75", 0.75), ("requires more than $0.74", 0.74)],
)
def test_the_rent_refusal_fills_required_and_available(phrase, required):
    # both server wordings: required is the amount printed in each
    detail = (
        f"Insufficient balance. This node costs $2.99/hour, so renting it {phrase} "
        "(15 minutes of runtime). Your balance is $0.70."
    )

    error = permission_error(detail, code="insufficient_balance")

    assert isinstance(error, LiumInsufficientBalanceError)
    assert error.required == pytest.approx(required)
    assert error.available == pytest.approx(0.70)


def test_a_refusal_without_an_amount_leaves_required_unset():
    error = permission_error("Insufficient balance", code="insufficient_balance")

    assert error.required is None
    assert error.available is None


@pytest.mark.parametrize(
    ("detail", "available"),
    [
        # the zero-balance refusal, personal and workspace wording
        (
            "Insufficient balance. Your balance is -$0.30; renting needs more. Top up at https://lium.io/billing.",
            -0.30,
        ),
        (
            "Insufficient balance. The workspace's billing owner pays for this and their balance is -$0.30; "
            "renting needs more. Ask them to top up at https://lium.io/billing.",
            -0.30,
        ),
        ("Insufficient balance. Your balance is $0.00; renting needs more.", 0.0),
        (
            "Insufficient balance. This node costs $2.99/hour, so renting it requires at least $0.75 "
            "(15 minutes of runtime). Your balance is $-0.30.",
            -0.30,
        ),
    ],
)
def test_a_negative_balance_keeps_its_sign(detail, available):
    error = permission_error(detail, code="insufficient_balance")

    assert error.available == pytest.approx(available)
