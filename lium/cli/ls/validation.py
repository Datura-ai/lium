"""Validation logic for ls command."""


def validate(
    sort_by: str,
    limit: int | None,
    lat: float | None,
    lon: float | None,
    max_distance: int | None,
) -> tuple[bool, str | None]:
    """Validate ls command options, returns (is_valid, error_message)."""

    # Validate sort_by
    valid_sort_options = ["download", "price_gpu", "price_total", "loc", "id", "gpu"]
    if sort_by not in valid_sort_options:
        return False, f"Invalid sort option: {sort_by}"

    # Validate limit
    if limit is not None and limit <= 0:
        return False, "Limit must be a positive integer"

    if (lat is None) ^ (lon is None):
        return False, "Both --lat and --lon must be provided together"

    if max_distance is not None and max_distance <= 0:
        return False, "--max-distance must be positive"

    if max_distance is not None and (lat is None or lon is None):
        return False, "--max-distance requires --lat and --lon"

    return True, None
