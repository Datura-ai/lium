from datetime import datetime

from rich.table import Table


def _format_status(status: str) -> str:
    status_upper = status.upper()
    if status_upper == "COMPLETED":
        return f"[green]{status}[/green]"
    if status_upper in ["FAILED", "ERROR"]:
        return f"[red]{status}[/red]"
    return f"[yellow]{status}[/yellow]"


def _format_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


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
    table.add_column("Progress", justify="right")
    table.add_column("Created")
    table.add_column("Restore Path")
    table.add_column("Error")

    for idx, log in enumerate(logs, 1):
        restore_id_full = getattr(log, "id", "unknown")
        status = _format_status(getattr(log, "status", "Unknown"))
        progress = getattr(log, "progress", None)
        progress_text = f"{progress:.0f}%" if progress is not None else ""
        created = _format_datetime(getattr(log, "created_at", None))
        restore_path = getattr(log, "restore_path", None) or ""
        error = getattr(log, "error_message", None) or ""

        table.add_row(
            str(idx),
            restore_id_full,
            status,
            progress_text,
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

    progress = getattr(log, "progress", None)
    if progress is not None:
        lines.append(f"Progress: {progress:.0f}%")

    restore_path = getattr(log, "restore_path", None)
    if restore_path:
        lines.append(f"Restore Path: {restore_path}")

    lines.append(f"Created: {getattr(log, 'created_at', 'Unknown')}")

    if getattr(log, "completed_at", None):
        lines.append(f"Completed: {log.completed_at}")

    if getattr(log, "error_message", None):
        lines.append(f"Error: {log.error_message}")

    return "\n".join(lines)
