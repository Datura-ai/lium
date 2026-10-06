"""Config edit command implementation."""

import click

from lium.cli.utils import CliFailure, EXIT_GENERAL_ERROR, handle_errors
from .actions import EditConfigAction


@click.command(name="edit")
@handle_errors
def config_edit_command():
    """Open configuration file in default editor."""

    # Execute
    ctx = {}

    action = EditConfigAction()
    result = action.execute(ctx)

    if not result.ok:
        raise CliFailure("editor_failed", result.error, EXIT_GENERAL_ERROR)
