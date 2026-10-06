"""Backup and restore lifecycle commands."""

import click

from lium.cli import ui
from lium.cli.utils import ensure_config, handle_errors
from lium.sdk import Lium


@click.command("cancel")
@click.option("--id", "backup_id", required=True, help="Active backup ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_cancel_command(backup_id: str, yes: bool) -> None:
    """Cancel an active backup and keep its history."""
    ensure_config()
    if not yes and not ui.confirm(f"Cancel backup '{backup_id}'?"):
        return

    client = Lium()
    resolved_backup_id = ui.load(
        "Resolving backup ID", lambda: client.resolve_backup_id(backup_id)
    )
    ui.load(
        "Requesting backup cancellation",
        lambda: client.backup_cancel(resolved_backup_id),
    )
    ui.success(
        "Backup cancellation requested. Its history and last reported progress will remain available."
    )


@click.command("delete")
@click.option("--id", "backup_id", required=True, help="Completed backup ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_delete_command(backup_id: str, yes: bool) -> None:
    """Delete stored data for a completed backup."""
    ensure_config()
    if not yes and not ui.confirm(
        f"Delete backup '{backup_id}'? This cannot be undone."
    ):
        return

    client = Lium()
    resolved_backup_id = ui.load(
        "Resolving backup ID", lambda: client.resolve_backup_id(backup_id)
    )
    ui.load(
        "Starting backup deletion", lambda: client.backup_log_delete(resolved_backup_id)
    )
    ui.success(
        "Backup deletion started. Storage usage will update after cleanup finishes."
    )


@click.command("restore-cancel")
@click.option("--id", "restore_id", required=True, help="Active restore ID")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_restore_cancel_command(restore_id: str, yes: bool) -> None:
    """Cancel an active restore."""
    ensure_config()
    if not yes and not ui.confirm(
        f"Cancel restore '{restore_id}'? Files already written to the restore directory may remain."
    ):
        return

    client = Lium()
    resolved_restore_id = ui.load(
        "Resolving restore ID", lambda: client.resolve_restore_id(restore_id)
    )
    ui.load(
        "Requesting restore cancellation",
        lambda: client.restore_cancel(resolved_restore_id),
    )
    ui.success(
        "Restore cancellation requested. Partial files may remain in the restore directory."
    )
