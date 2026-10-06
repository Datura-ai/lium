"""Validation logic for bk restore-logs command."""


def validate(pod_id: str | None, restore_id: str | None) -> tuple[bool, str]:
    """Validate bk restore-logs arguments."""
    if not pod_id and not restore_id:
        return False, "Please specify either a pod ID or use --id for a specific restore"

    return True, ""
