from datetime import datetime
from rich.table import Table


def format_logs_table(logs: list) -> Table:
    """Format logs as a table."""
    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        padding=(0, 2)
    )

    table.add_column("#", style="dim")
    table.add_column("Backup ID", style="cyan")
    table.add_column("Status")
    table.add_column("Progress", justify="right")
    table.add_column("Created")
    table.add_column("Error")

    for idx, log in enumerate(logs, 1):
        backup_id_full = getattr(log, 'id', 'unknown')
        status = getattr(log, 'status', 'Unknown')

        # Color status
        if status.upper() == 'COMPLETED':
            status = f"[green]{status}[/green]"
        elif status.upper() in ['FAILED', 'ERROR']:
            status = f"[red]{status}[/red]"
        else:
            status = f"[yellow]{status}[/yellow]"

        created = getattr(log, 'created_at', 'Unknown')
        if created != 'Unknown':
            try:
                dt = datetime.fromisoformat(created.replace('Z', '+00:00'))
                created = dt.strftime('%Y-%m-%d %H:%M')
            except:
                pass

        progress = getattr(log, 'progress', None)
        progress_text = f"{progress:.0f}%" if progress is not None else ""
        error = getattr(log, 'error_message', None) or ""

        table.add_row(
            str(idx),
            backup_id_full,
            status,
            progress_text,
            created,
            error
        )

    return table


def format_single_backup(pod_name: str, log) -> str:
    """Format single backup details."""
    lines = [f"Pod: {pod_name}"]
    lines.append(f"Status: {getattr(log, 'status', 'Unknown')}")
    progress = getattr(log, 'progress', None)
    if progress is not None:
        lines.append(f"Progress: {progress:.0f}%")
    lines.append(f"Created: {getattr(log, 'created_at', 'Unknown')}")

    if hasattr(log, 'completed_at') and log.completed_at:
        lines.append(f"Completed: {log.completed_at}")

    if hasattr(log, 'size_bytes') and log.size_bytes:
        size_mb = log.size_bytes / (1024 * 1024)
        lines.append(f"Size: {size_mb:.2f} MB")

    if hasattr(log, 'error_message') and log.error_message:
        lines.append(f"Error: {log.error_message}")

    return "\n".join(lines)
