"""Bk now command implementation."""

from typing import Optional

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from . import validation, parsing
from .actions import TriggerBackupAction


@click.command("now")
@click.argument("pod_id")
@click.option("-n", "--name", help="Backup name (e.g., 'pre-release')")
@click.option("-d", "--description", help="Backup description (e.g., 'before deploy')")
@handle_errors
def bk_now_command(pod_id: str, name: Optional[str], description: Optional[str]):
    """Trigger an immediate backup for a pod."""
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id)
    if not valid:
        ui.error(error)
        return

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        ui.warning("No active pods")
        return

    # Parse
    parsed, error = parsing.parse(pod_id, name, description, all_pods)
    if error:
        ui.error(error)
        return

    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")
    backup_name = parsed.get("name")
    backup_description = parsed.get("description")

    # Execute
    ctx = {
        "lium": lium,
        "pod": pod,
        "pod_name": pod_name,
        "name": backup_name,
        "description": backup_description
    }

    action = TriggerBackupAction()
    result = ui.load(f"Triggering backup '{backup_name}'", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    backup = result.data.get("backup") or {}
    backup_log_id = backup.get("backup_log_id")
    if backup_log_id:
        ui.success(f"Backup started for {pod_name} (id={backup_log_id[:8]})")
    else:
        ui.success(f"Backup started for {pod_name}")
