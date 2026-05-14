"""Bk set command implementation."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from . import validation, parsing
from .actions import SetBackupAction


@click.command("set")
@click.argument("pod_id")
@click.option("--path", default="/root", help="Backup path (default: /root)")
@click.option("--every", help="Backup frequency (e.g., 1h, 6h, 24h)")
@click.option("--keep", help="Retention period (e.g., 1d, 7d, 30d)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_set_command(pod_id: str, path: str, every: str, keep: str, yes: bool):
    """Set or update backup configuration for a pod.

    \b
    POD_ID: Pod identifier - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    \b
    Examples:
      lium bk set 1 --path /root --every 6h --keep 7d
      lium bk set eager-wolf-aa --every 1h --keep 1d
    """
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id, every, keep)
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
    parsed, error = parsing.parse(pod_id, path, every, keep, all_pods)
    if error:
        ui.error(error)
        return

    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")
    backup_path = parsed.get("path")
    frequency_hours = parsed.get("frequency_hours")
    retention_days = parsed.get("retention_days")

    # Execute
    ctx = {
        "lium": lium,
        "pod": pod,
        "pod_name": pod_name,
        "path": backup_path,
        "frequency_hours": frequency_hours,
        "retention_days": retention_days
    }

    action = SetBackupAction()
    result = ui.load("Setting backup configuration", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    ui.success(
        f"Backup configured for {pod_name}: "
        f"path={backup_path}, every={frequency_hours}h, keep={retention_days}d"
    )
