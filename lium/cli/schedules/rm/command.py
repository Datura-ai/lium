"""Schedules rm command implementation."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    batch_refusal_failure,
    handle_errors,
    json_output_requested,
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

    budget = list(zip(result.data.get("budget_huids") or [], result.data.get("budget_errors") or []))
    funding = list(zip(result.data.get("funding_huids") or [], result.data.get("funding_errors") or []))
    if budget or funding:
        # report what went through first, then exit 6 naming every refused pod by kind (as in `rm --in`); a JSON
        # reader gets the same report inside the one error envelope, so no prose goes to stdout then
        cancelled, failures = result.data.get("cancelled") or [], result.data.get("failures") or []
        if not json_output_requested():
            if cancelled:
                ui.success(f"Cancelled schedules for {', '.join(cancelled)}")
            if failures:
                ui.error(f"Failed to cancel schedules: {', '.join(failures)}")
        raise batch_refusal_failure(budget, funding, done=("cancelled", cancelled), failed=failures)
    # Only error if anything failed
    if not result.ok:
        failures = result.data.get("failures", [])
        raise CliFailure(
            "schedule_cancel_failed",
            f"Failed to cancel schedules: {', '.join(failures)}",
            EXIT_GENERAL_ERROR,
        )
