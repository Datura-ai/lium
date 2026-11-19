"""The rent path's 403 detail fills LiumInsufficientBalanceError's required and available amounts."""

import pytest

from lium.sdk.client import permission_error
from lium.sdk.exceptions import LiumInsufficientBalanceError


@pytest.mark.parametrize(
    ("detail", "exclusive"),
    [
        (
            "Insufficient balance. This node costs $2.99/hour, so renting it requires more than $0.74 "
            "(15 minutes of runtime). Your balance is $0.70.",
            True,
        ),
        (
            # an older server's wording stays parseable
            "Insufficient balance. This node costs $2.99/hour, so renting it requires at least $0.74 "
            "(15 minutes of runtime). Your balance is $0.70.",
            False,
        ),
    ],
)
def test_the_rent_refusal_fills_required_and_available(detail, exclusive):
    error = permission_error(detail, code="insufficient_balance")

    assert isinstance(error, LiumInsufficientBalanceError)
    assert error.required == pytest.approx(0.74)
    assert error.available == pytest.approx(0.70)
    assert error.required_exclusive is exclusive


def test_a_refusal_without_an_amount_leaves_required_exclusive_unset():
    error = permission_error("Insufficient balance", code="insufficient_balance")

    assert error.required is None
    assert error.required_exclusive is None
