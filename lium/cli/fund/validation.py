"""Fund command validation."""


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
