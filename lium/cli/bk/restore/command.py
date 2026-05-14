import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from . import validation, parsing
from .actions import RestoreBackupAction


@click.command("restore")
@click.argument("pod_id")
@click.option("--id", "backup_id", required=True, help="Backup ID to restore")
@click.option("--to", "restore_path", default="/root", help="Restore path (default: /root)")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_restore_command(pod_id: str, backup_id: str, restore_path: str, yes: bool):
    """Restore a backup to a pod."""
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id, backup_id)
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
    pod_name = parsed.get("pod_name")

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
    result = ui.load(f"Restoring backup to {restore_path}", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    ui.success(f"Restore started for {pod_name} at {restore_path}")
