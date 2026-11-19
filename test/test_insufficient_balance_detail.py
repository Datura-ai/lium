"""The rent path's 403 detail fills LiumInsufficientBalanceError's required and available amounts."""

import pytest

from lium.sdk.client import permission_error
from lium.sdk.exceptions import LiumInsufficientBalanceError


def test_the_rent_refusal_fills_required_and_available():
    detail = (
        "Insufficient balance. This node costs $2.99/hour, so renting it requires at least $0.75 "
        "(15 minutes of runtime). Your balance is $0.70."
    )

    error = permission_error(detail, code="insufficient_balance")

    assert isinstance(error, LiumInsufficientBalanceError)
    assert error.required == pytest.approx(0.75)
    assert error.available == pytest.approx(0.70)


def test_a_refusal_without_an_amount_leaves_required_unset():
    error = permission_error("Insufficient balance", code="insufficient_balance")

    assert error.required is None
    assert error.available is None
