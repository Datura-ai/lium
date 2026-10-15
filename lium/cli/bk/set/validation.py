"""Validation logic for bk set command."""

import re

# the backend's limits (BackupConfigurationCreateRequest / UpdateRequest)
MAX_FREQUENCY_HOURS = 168
MAX_RETENTION_DAYS = 365


def validate(pod_id: str, every: str | None, keep: str | None, path: str | None = None) -> tuple[bool, str]:
    """Validate bk set arguments."""
    if not pod_id or not pod_id.strip():
        return False, "Pod ID required"

    if every:
        match = re.fullmatch(r'(\d+)([hd])', every)
        hours = int(match.group(1)) * (24 if match.group(2) == 'd' else 1) if match else 0
        if not 1 <= hours <= MAX_FREQUENCY_HOURS:
            return False, "Invalid frequency. Use 1h to 168h (7d), e.g. '6h' or '1d'"

    if keep:
        match = re.fullmatch(r'(\d+)d', keep)
        if not match or not 1 <= int(match.group(1)) <= MAX_RETENTION_DAYS:
            return False, "Invalid retention. Use 1d to 365d, e.g. '7d'"

    if path and ".." in path:
        return False, "Backup path cannot contain '..'"

    return True, ""
