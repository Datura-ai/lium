"""Validation logic for logs command."""


def validate(
    pod_id: str | None,
    tail: int,
) -> tuple[bool, str | None]:
    """Validate logs command options, returns (is_valid, error_message)."""
    if not pod_id:
        return False, "POD_ID is required"

    if tail <= 0:
        return False, "Tail must be a positive integer"

    return True, None
