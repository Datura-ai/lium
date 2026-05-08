"""Fund command validation."""

import math


def validate_amount(amount_str: str) -> tuple[float, str]:
    """Validate TAO amount.

    Returns:
        (amount, error_message) - amount is 0.0 if invalid
    """
    try:
        amount = float(amount_str)
        if amount <= 0:
            return 0.0, "Amount must be positive"
        return amount, ""
    except ValueError:
        return 0.0, "Invalid amount format"


def validate_usd_amount(amount_str: str) -> tuple[float, str]:
    """Validate USD amount for a crypto invoice."""
    try:
        amount = float(amount_str)
    except ValueError:
        return 0.0, "Invalid amount format"

    if not math.isfinite(amount):
        return 0.0, "Amount must be finite"
    if amount <= 0:
        return 0.0, "Amount must be positive"
    return amount, ""


def validate_currency(currency: str) -> tuple[str, str]:
    """Validate and normalize a NowPayments currency code."""
    normalized = currency.strip().lower()
    if not normalized:
        return "", "Currency is required"
    return normalized, ""
