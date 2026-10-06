"""Backup path warnings shared by backup commands."""


def entire_volume_backup_warning(pod, backup_path: str) -> str | None:
    volume_path = getattr(pod, "volume_path", None)
    if not volume_path:
        volumes = (getattr(pod, "template", None) or {}).get("volumes", [])
        volume_path = volumes[0] if volumes else "/root"

    if not backup_path or backup_path.rstrip("/") == volume_path.rstrip("/"):
        return (
            "This backs up the entire volume. For more reliable backups, prefer a stable subdirectory "
            "that is not being actively changed."
        )
    return None
