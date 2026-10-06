"""Config unset command implementation."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors
from .actions import UnsetConfigAction


@click.command(name="unset")
@click.argument("key")
@handle_errors
def config_unset_command(key: str):
    """Remove a configuration value."""

    # Execute
    ctx = {"key": key}

    action = UnsetConfigAction()
    result = action.execute(ctx)

    # Clearing a key that is already absent leaves the config in the asked-for
    # state, so it stays a success — the same call as `rm --all` on no pods.
    if not result.ok:
        ui.warning(result.error)
