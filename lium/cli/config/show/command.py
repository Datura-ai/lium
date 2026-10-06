"""Config show command implementation."""

import click
from rich.markup import escape

from lium.cli import ui
from lium.cli.utils import handle_errors
from .actions import ShowConfigAction


@click.command(name="show")
@handle_errors
def config_show_command():
    """Show the entire configuration.

    API keys and the session token are shortened to their first 8 and last 4 characters, as in
    `config get`; the first line names the file that holds the full values.
    """

    # Execute
    ctx = {}

    action = ShowConfigAction()
    result = action.execute(ctx)

    config_path = result.data.get("config_path")
    content = result.data.get("content")

    ui.dim(f"# {config_path}")
    if content:
        ui.print(content, markup=False, highlight=False)
    note = result.data.get("note")
    if note:
        ui.dim(f"# {escape(note)}")
