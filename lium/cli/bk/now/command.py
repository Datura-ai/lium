"""Bk now command implementation."""

from typing import Optional

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    ensure_config,
)
from . import validation, parsing
from .actions import TriggerBackupAction
from ..path_warning import entire_volume_backup_warning


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
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(pod_id, name, description, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")
    backup_name = parsed.get("name")
    backup_description = parsed.get("description")
    backup_config = ui.load(
        "Loading backup configuration", lambda: lium.backup_config(pod)
    )
    if not backup_config:
        raise CliFailure(
            "no_backup_config", "No backup configuration found", EXIT_GENERAL_ERROR
        )
    path_warning = entire_volume_backup_warning(
        pod, getattr(backup_config, "backup_path", "")
    )
    if path_warning:
        ui.warning(path_warning)

    # Execute
    ctx = {
        "lium": lium,
        "pod": pod,
        "pod_name": pod_name,
        "name": backup_name,
        "description": backup_description,
        "backup_config": backup_config,
    }

    action = TriggerBackupAction()
    result = ui.load(f"Triggering backup '{backup_name}'", lambda: action.execute(ctx))

    if not result.ok:
        raise CliFailure("no_backup_config", result.error, EXIT_GENERAL_ERROR)

    backup = result.data.get("backup") or {}
    backup_log_id = backup.get("backup_log_id")
    if backup_log_id:
        ui.success(f"Backup started for {pod_name} (id={backup_log_id[:8]})")
    else:
        ui.success(f"Backup started for {pod_name}")
