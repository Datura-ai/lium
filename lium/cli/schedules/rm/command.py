"""Schedules rm command implementation."""

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
from .actions import CancelSchedulesAction


@click.command("rm")
@click.argument("indices")
@handle_errors
def schedules_rm_command(indices: str):
    """Cancel scheduled terminations by index."""

    # Validate
    valid, error = validation.validate(indices)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading scheduled terminations", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(indices, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    pods_to_cancel = parsed.get("pods_to_cancel")

    # Execute
    ctx = {"pods": pods_to_cancel, "lium": lium}

    action = CancelSchedulesAction()
    result = action.execute(ctx)

    # Only error if anything failed
    if not result.ok:
        failed_huids = result.data.get("failed_huids", [])
        raise CliFailure(
            "schedule_cancel_failed",
            f"Failed to cancel schedules: {', '.join(failed_huids)}",
            EXIT_GENERAL_ERROR,
        )
