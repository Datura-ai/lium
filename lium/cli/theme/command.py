"""Theme command implementation."""

import click

from lium.cli.themed_console import ThemedConsole
from lium.cli.utils import handle_errors
from .actions import SwitchThemeAction


@click.command("theme")
@click.argument("theme_name", type=click.Choice(["dark", "light"]))
@handle_errors
def theme_command(theme_name: str):
    """Set CLI color theme (dark or light).

    \b
    Examples:
      lium theme dark
      lium theme light
    """
    console = ThemedConsole()

    ctx = {"console": console, "theme_name": theme_name}
    action = SwitchThemeAction()
    action.execute(ctx)
