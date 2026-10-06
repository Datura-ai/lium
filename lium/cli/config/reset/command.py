"""Config reset command implementation."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors
from .actions import ResetConfigAction


@click.command(name="reset")
@click.option("--yes", "-y", "--confirm", "confirm", is_flag=True, help="Skip confirmation prompt.")
@handle_errors
def config_reset_command(confirm: bool):
    """Reset configuration to defaults."""

    # Confirm
    if not confirm:
        if not ui.confirm("This will delete all configuration. Continue?", default=False):
            return

    # Execute
    ctx = {}

    action = ResetConfigAction()
    result = action.execute(ctx)

    if result.error:
        ui.warning(result.error)
