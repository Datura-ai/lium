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

    budget_errors = result.data.get("budget_errors") or []
    funding_errors = result.data.get("funding_errors") or []
    refusals = budget_errors + funding_errors
    if refusals:
        # report what went through first, then exit 6 with the first refusal; in a mixed batch the no-balance pods are
        # named with their hint too, since raising the key's budget does not fund the account (as in `rm --in`)
        if result.data.get("cancelled"):
            ui.success(f"Cancelled schedules for {', '.join(result.data['cancelled'])}")
        if result.data.get("failures"):
            ui.error(f"Failed to cancel schedules: {', '.join(result.data['failures'])}")
        if budget_errors and funding_errors:
            funding_huids = ", ".join(result.data["funding_huids"])
            ui.error(f"No balance on the account for pods: {funding_huids} ({funding_errors[0].hint})")
        raise refusals[0]
    # Only error if anything failed
    if not result.ok:
        failures = result.data.get("failures", [])
        raise CliFailure(
            "schedule_cancel_failed",
            f"Failed to cancel schedules: {', '.join(failures)}",
            EXIT_GENERAL_ERROR,
        )
