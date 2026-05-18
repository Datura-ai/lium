from typing import Optional

import click

from lium.cli import ui
from lium.cli.utils import ensure_config, handle_errors
from lium.sdk import Lium

from . import display, parsing, validation
from .actions import GetRestoreLogsAction


@click.command("restore-logs")
@click.argument("pod_id", required=False)
@click.option("--id", "restore_id", help="Specific restore ID to show details")
@handle_errors
def bk_restore_logs_command(pod_id: Optional[str], restore_id: Optional[str]):
    """Show restore logs for a pod or specific restore."""
    ensure_config()

    valid, error = validation.validate(pod_id, restore_id)
    if not valid:
        ui.error(error)
        return

    lium = Lium()

    if restore_id:
        ctx = {"lium": lium, "restore_id": restore_id}
        action = GetRestoreLogsAction()
        result = ui.load("Loading restore details", lambda: action.execute(ctx))

        if not result.ok:
            ui.error(result.error)
            return

        pod_name_found = result.data.get("pod_name")
        log = result.data.get("log")
        ui.print(display.format_single_restore(pod_name_found, log))
        return

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

        action = GetRestoreLogsAction()
        return action.execute(ctx), None

    result, error = ui.load("Loading restore logs", load_logs)

    if error:
        ui.error(error)
        return

    if result and result.error:
        ui.error(result.error)
        return

    logs = result.data.get("logs")

    if not logs:
        ui.warning(f"No restore logs found for pod '{pod_id}'")
        return

    table = display.format_logs_table(logs)
    ui.print(table, highlight=True)
