from typing import Optional

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    ensure_config,
)
from . import validation, parsing
from .actions import RestoreBackupAction


@click.command("restore")
@click.argument("pod_id")
@click.option("--id", "backup_id", required=True, help="Backup ID to restore")
@click.option(
    "--to",
    "restore_path",
    help="New or empty restore subdirectory (default: <pod volume>/restored)",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_restore_command(pod_id: str, backup_id: str, restore_path: Optional[str], yes: bool):
    """Restore a backup to a pod."""
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id, backup_id)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(pod_id, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")
    restore_path = restore_path or pod.default_restore_path

    # Confirm
    if not yes:
        if not ui.confirm(f"Restore backup to pod '{pod_id}' at {restore_path}?"):
            return

    # Execute
    ctx = {
        "lium": lium,
        "pod": pod,
        "pod_name": pod_name,
        "backup_id": backup_id,
        "restore_path": restore_path
    }

    action = RestoreBackupAction()
    ui.load(f"Restoring backup to {restore_path}", lambda: action.execute(ctx))

    ui.success(f"Restore started for {pod_name} at {restore_path}")
    ui.warning(
        f"Do not add, modify, or remove files in {restore_path} until the restore completes. "
        f"Check progress with: lium bk restore-logs {pod_name}"
    )
