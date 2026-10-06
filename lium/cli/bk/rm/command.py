"""Bk rm command implementation."""

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
from .actions import RemoveBackupAction


@click.command("rm")
@click.argument("pod_id")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def bk_rm_command(pod_id: str, yes: bool):
    """Remove backup configuration for a pod.

    \b
    POD_ID: Pod identifier - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    \b
    Examples:
      lium bk rm 1                  # Remove backup for pod #1
      lium bk rm eager-wolf-aa      # Remove backup by name
      lium bk rm 1 --yes            # Remove without confirmation
    """
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
    parsed, error = parsing.parse(pod_id, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    pod = parsed.get("pod")
    pod_name = parsed.get("pod_name")

    # Confirm
    if not yes:
        if not ui.confirm(f"Remove backup configuration for pod '{pod_id}'?"):
            return

    # Execute
    ctx = {"lium": lium, "pod": pod, "pod_name": pod_name}

    action = RemoveBackupAction()
    result = ui.load("Removing backup configuration", lambda: action.execute(ctx))

    if not result.ok:
        raise CliFailure("no_backup_config", result.error, EXIT_GENERAL_ERROR)

    ui.success(f"Backup configuration removed for {pod_name}")
