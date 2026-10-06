from datetime import datetime

from rich.table import Table

_STAGE_LABELS = {
    "WAITING_FOR_POD": "Waiting for pod",
    "PREPARING": "Preparing",
    "RESTORING": "Restoring",
    "FINALIZING": "Finalizing",
}


def _format_status(status: str) -> str:
    status_upper = status.upper()
    if status_upper == "COMPLETED":
        return f"[green]{status}[/green]"
    if status_upper in ["FAILED", "ERROR"]:
        return f"[red]{status}[/red]"
    if status_upper == "CANCELLED":
        return f"[dim]{status}[/dim]"
    return f"[yellow]{status}[/yellow]"


def _format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def _format_progress(value: float | None) -> str:
    if value is None:
        return ""
    return f"{value:.2f}".rstrip("0").rstrip(".") + "%"


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{int(value)} B"


def _format_stage(log) -> str:
    if getattr(log, "status", "").upper() in {"COMPLETED", "FAILED", "CANCELLED"}:
        return ""
    stage = getattr(log, "stage", None)
    return _STAGE_LABELS.get(stage, stage or "")


def _format_work(log) -> str:
    if getattr(log, "status", "").upper() in {"COMPLETED", "FAILED", "CANCELLED"}:
        return ""

    stage = getattr(log, "stage", None)
    total_files = getattr(log, "total_files", None)
    processed_files = getattr(log, "processed_files", None)
    total_bytes = getattr(log, "total_bytes", None)
    processed_bytes = getattr(log, "processed_bytes", None)

    if stage == "PREPARING" and total_files is not None:
        return f"{total_files:,} files discovered"

    details = []
    if processed_files is not None and total_files is not None:
        details.append(f"{processed_files:,}/{total_files:,} files")
    if processed_bytes is not None and total_bytes is not None:
        details.append(f"{_format_bytes(processed_bytes)}/{_format_bytes(total_bytes)}")
    throughput = getattr(log, "throughput_bytes_per_second", None)
    if throughput:
        details.append(f"{_format_bytes(throughput)}/s")
    remaining = getattr(log, "estimated_remaining_seconds", None)
    if remaining:
        details.append(f"~{_format_duration(remaining)} left")
    return ", ".join(details)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def format_logs_table(logs: list) -> Table:
    """Format restore logs as a table."""
    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        padding=(0, 2),
    )

    table.add_column("#", style="dim")
    table.add_column("Restore ID", style="cyan")
    table.add_column("Status")
    table.add_column("Stage")
    table.add_column("Progress", justify="right")
    table.add_column("Work")
    table.add_column("Created")
    table.add_column("Restore Path")
    table.add_column("Error")

    for idx, log in enumerate(logs, 1):
        restore_id_full = getattr(log, "id", "unknown")
        status = _format_status(getattr(log, "status", "Unknown"))
        progress_text = _format_progress(getattr(log, "progress", None))
        created = _format_datetime(getattr(log, "created_at", None))
        restore_path = getattr(log, "restore_path", None) or ""
        error = getattr(log, "error_message", None) or ""

        table.add_row(
            str(idx),
            restore_id_full,
            status,
            _format_stage(log),
            progress_text,
            _format_work(log),
            created,
            restore_path,
            error,
        )

    return table


def format_single_restore(pod_name: str, log) -> str:
    """Format single restore details."""
    lines = [f"Pod: {pod_name}"]
    lines.append(f"Restore ID: {getattr(log, 'id', 'unknown')}")
    lines.append(f"Backup ID: {getattr(log, 'backup_id', 'unknown')}")
    lines.append(f"Status: {getattr(log, 'status', 'Unknown')}")

    restore_mode = getattr(log, "restore_mode", None)
    if restore_mode:
        lines.append(f"Mode: {restore_mode}")

    stage = _format_stage(log)
    if stage:
        lines.append(f"Stage: {stage}")

    progress = getattr(log, "progress", None)
    if progress is not None:
        lines.append(f"Progress: {_format_progress(progress)}")

    work = _format_work(log)
    if work:
        lines.append(f"Work: {work}")

    elapsed = getattr(log, "elapsed_seconds", None)
    if elapsed is not None:
        lines.append(f"Elapsed: {_format_duration(elapsed)}")

    restore_path = getattr(log, "restore_path", None)
    if restore_path:
        lines.append(f"Restore Path: {restore_path}")

    lines.append(f"Created: {getattr(log, 'created_at', 'Unknown')}")

    if getattr(log, "completed_at", None):
        lines.append(f"Completed: {log.completed_at}")

    if getattr(log, "error_message", None):
        lines.append(f"Error: {log.error_message}")

    return "\n".join(lines)
