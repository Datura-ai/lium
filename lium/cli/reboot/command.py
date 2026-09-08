"""Reboot command implementation."""

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
)
from . import validation, parsing
from .actions import RebootPodsAction


@click.command("reboot")
@click.argument("targets", required=False)
@click.option("--all", "-a", is_flag=True, help="Reboot all active pods")
@click.option("--volume-id", help="Volume ID to attach when rebooting")
@click.option("--yes", "-y", is_flag=True, help="Accepted for symmetry with rm and up; reboot never prompts")
@handle_errors
def reboot_command(targets: Optional[str], all: bool, volume_id: Optional[str], yes: bool):
    """Reboot GPU pods.

    \b
    TARGETS: a pod name, huid, id or index from 'lium ps'; comma-separated
    for several; or --all.
    \b
    Examples:
      lium reboot my-pod
      lium reboot 1,2
      lium reboot all
      lium reboot my-pod --volume-id <VOLUME_ID>
    """

    # Validate
    valid, error = validation.validate(targets, all)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    # --all against an empty account is an idempotent no-op; a named target is not.
    if not all_pods:
        if all:
            ui.warning("No active pods")
            return
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(targets, all, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    selected_pods = parsed.get("selected_pods")

    # Execute
    payload_volume_id = volume_id.strip() if volume_id else None
    ctx = {"pods": selected_pods, "lium": lium, "volume_id": payload_volume_id}

    action = RebootPodsAction()
    result = action.execute(ctx)

    # Only error if anything failed
    if not result.ok:
        failed_huids = result.data.get("failed_huids", [])
        raise CliFailure(
            "reboot_failed",
            f"Failed to reboot pods: {', '.join(failed_huids)}",
            EXIT_GENERAL_ERROR,
        )
