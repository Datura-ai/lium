from datetime import datetime
from rich.table import Table


def _format_status(status: str) -> str:
    status_upper = status.upper()
    if status_upper == "COMPLETED":
        return f"[green]{status}[/green]"
    if status_upper in {"FAILED", "ERROR"}:
        return f"[red]{status}[/red]"
    if status_upper in {"CANCELLED", "SKIPPED", "EXPIRED", "DELETED"}:
        return f"[dim]{status}[/dim]"
    return f"[yellow]{status}[/yellow]"


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
    return f"{value} B"


def _format_duration(seconds: int | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds}s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {remaining_seconds}s"
    hours, remaining_minutes = divmod(minutes, 60)
    return f"{hours}h {remaining_minutes}m"


def _format_work(log) -> str:
    details = []
    processed_bytes = getattr(log, "processed_bytes", None)
    total_bytes = getattr(log, "total_bytes", None)
    if processed_bytes is not None and total_bytes is not None:
        details.append(f"{_format_bytes(processed_bytes)}/{_format_bytes(total_bytes)}")
    processed_files = getattr(log, "processed_files", None)
    total_files = getattr(log, "total_files", None)
    if processed_files is not None and total_files is not None:
        details.append(f"{processed_files:,}/{total_files:,} files")
    throughput = getattr(log, "throughput_bytes_per_second", None)
    if throughput:
        details.append(f"{_format_bytes(throughput)}/s")
    remaining = getattr(log, "estimated_remaining_seconds", None)
    if remaining:
        details.append(f"~{_format_duration(remaining)} left")
    return ", ".join(details)


def format_logs_table(logs: list) -> Table:
    """Format logs as a table."""
    table = Table(show_header=True, header_style="dim", box=None, padding=(0, 2))

    table.add_column("#", style="dim")
    table.add_column("Backup ID", style="cyan")
    table.add_column("Status")
    table.add_column("Progress", justify="right")
    table.add_column("Created")
    table.add_column("Work")
    table.add_column("Details")

    for idx, log in enumerate(logs, 1):
        backup_id_full = getattr(log, "id", "unknown")
        status = getattr(log, "status", "Unknown")

        status = _format_status(status)

        created = getattr(log, "created_at", "Unknown")
        if created != "Unknown":
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                created = dt.strftime("%Y-%m-%d %H:%M")
            except:
                pass

        progress = getattr(log, "progress", None)
        progress_text = _format_progress(progress)
        details = (
            getattr(log, "error_message", None)
            or getattr(log, "status_message", None)
            or ""
        )

        table.add_row(
            str(idx),
            backup_id_full,
            status,
            progress_text,
            created,
            _format_work(log),
            details,
        )

    return table


def format_single_backup(log) -> str:
    """Format single backup details."""
    lines = [f"Backup ID: {getattr(log, 'id', 'Unknown')}"]
    lines.append(f"Status: {getattr(log, 'status', 'Unknown')}")
    progress = getattr(log, "progress", None)
    if progress is not None:
        lines.append(f"Progress: {_format_progress(progress)}")
    lines.append(f"Created: {getattr(log, 'created_at', 'Unknown')}")

    if hasattr(log, "completed_at") and log.completed_at:
        lines.append(f"Completed: {log.completed_at}")

    work = _format_work(log)
    if work:
        lines.append(f"Work: {work}")

    elapsed = getattr(log, "elapsed_seconds", None)
    if elapsed is not None:
        lines.append(f"Elapsed: {_format_duration(elapsed)}")

    if hasattr(log, "error_message") and log.error_message:
        lines.append(f"Error: {log.error_message}")
    elif getattr(log, "status_message", None):
        lines.append(f"Details: {log.status_message}")

    return "\n".join(lines)
