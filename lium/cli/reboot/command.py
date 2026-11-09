"""Reboot command implementation."""

from typing import Optional
from uuid import UUID
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
    resolve_volume_huid,
)
from . import validation, parsing
from .actions import RebootPodsAction


@click.command("reboot")
@click.argument("targets", required=False)
@click.option("--all", "-a", is_flag=True, help="Reboot all active pods")
@click.option("--volume-id", help="Volume to attach when rebooting: its HUID (as `lium volumes` shows) or its ID")
@handle_errors
def reboot_command(targets: Optional[str], all: bool, volume_id: Optional[str]):
    """Reboot GPU pods."""

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
    payload_volume_id = _volume_api_id(volume_id.strip()) if volume_id and volume_id.strip() else None
    ctx = {"pods": selected_pods, "lium": lium, "volume_id": payload_volume_id}

    action = RebootPodsAction()
    result = action.execute(ctx)

    rebooted = result.data.get("rebooted", [])
    if not result.ok:
        # a batch that partly failed still names what was rebooted, so a retry targets only the failures
        done = f"Reboot requested for {', '.join(rebooted)}. " if rebooted else ""
        raise CliFailure(
            "reboot_failed",
            f"{done}Failed to reboot pods: {', '.join(result.data.get('failures', []))}",
            EXIT_GENERAL_ERROR,
        )
    ui.success(f"Reboot requested for {', '.join(rebooted)}")


def _volume_api_id(volume: str) -> str:
    """The API takes the volume's ID; `lium volumes` shows its HUID, so accept either."""
    try:
        UUID(volume)
        return volume
    except ValueError:
        pass
    volume_id = resolve_volume_huid(volume)
    if not volume_id:
        raise CliFailure(
            "invalid_arguments",
            f"Volume with HUID '{volume}' not found in your volumes (see 'lium volumes').",
            EXIT_CONFIGURATION_ERROR,
        )
    return volume_id
