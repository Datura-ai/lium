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
from . import validation, parsing, display
from .actions import GetBackupLogsAction


@click.command("logs")
@click.argument("pod_id", required=False)
@click.option("--id", "backup_id", help="Specific backup ID to show details")
@handle_errors
def bk_logs_command(pod_id: Optional[str], backup_id: Optional[str]):
    """Show backup logs for a pod or specific backup."""
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id, backup_id)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    lium = Lium()

    if backup_id:
        ctx = {"lium": lium, "backup_id": backup_id}

        action = GetBackupLogsAction()
        result = ui.load("Loading backup details", lambda: action.execute(ctx))

        if not result.ok:
            raise CliFailure("backup_not_found", result.error, EXIT_GENERAL_ERROR)

        log = result.data.get("log")

        output = display.format_single_backup(log)
        ui.print(output)
        return

    # Execute - load and parse in one go
    def load_logs():
        all_pods = lium.ps()
        if not all_pods:
            return None, "No active pods"

        parsed, error = parsing.parse(pod_id, all_pods)
        if error:
            return None, error

        pod = parsed.get("pod")
        pod_name = parsed.get("pod_name")
        ctx = {"lium": lium, "pod": pod, "pod_name": pod_name}

        action = GetBackupLogsAction()
        return action.execute(ctx), None

    result, error = ui.load("Loading backup logs", load_logs)

    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    logs = result.data.get("logs")

    if not logs:
        ui.warning(f"No backup logs found for pod '{pod_id}'")
        return

    # Display
    table = display.format_logs_table(logs)
    ui.print(table, highlight=True)
