"""Bk show command implementation."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from . import validation, parsing
from .actions import ShowBackupAction


@click.command("show")
@click.argument("pod_id")
@handle_errors
def bk_show_command(pod_id: str):
    """Show backup configuration for a specific pod.

    \b
    POD_ID: Pod identifier - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    \b
    Examples:
      lium bk show 1                 # Show backup config for pod #1
      lium bk show eager-wolf-aa     # Show backup config by name
    """
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
    parsed, error = parsing.parse(pod_id, all_pods)
    if error:
        ui.error(error)
        return

    pod = parsed.get("pod")
    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")

    # Execute
    ctx = {"lium": lium, "pod": pod, "pod_name": pod_name}

    action = ShowBackupAction()
    result = ui.load("Loading backup config", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    if not result.data.get("has_config"):
        ui.warning(f"No backup configuration found for {pod_name}")
        return

    # Display
    backup_path = result.data.get("backup_path")
    frequency_hours = result.data.get("frequency_hours")
    retention_days = result.data.get("retention_days")

    ui.print(f"Config: path={backup_path}, every={frequency_hours}h, keep={retention_days}d")

    last_backup = result.data.get("last_backup")
    if last_backup:
        ui.print(f"Last Backup: {last_backup['status']} at {last_backup['timestamp']} (id={last_backup['id']})")

    next_due = result.data.get("next_due")
    if next_due:
        ui.print(f"Next Due: {next_due}")
