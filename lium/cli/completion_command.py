"""`lium completion`: print or install shell tab completion."""

import click
from rich.markup import escape

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, handle_errors
from .completion import SHELLS, completion_script, install_completion


@click.command("completion")
@click.argument("shell", type=click.Choice(sorted(SHELLS)), required=False)
@click.option("--install", is_flag=True, help="Append the line to the shell's rc file instead of printing it")
@handle_errors
def completion_command(shell, install):
    """Print the shell line that enables tab completion for lium, or install it.

    Without SHELL the current $SHELL is used. Printing is the default so the
    line can be reviewed or placed by hand; --install appends it to ~/.bashrc,
    ~/.zshrc or ~/.config/fish/config.fish when it is not there yet.

    \b
    Examples:
      lium completion zsh                     # print the line for zsh
      lium completion --install               # install for the current shell
      lium completion bash >> ~/.bashrc       # same thing, by hand
    """
    import os

    shell = shell or os.path.basename(os.environ.get("SHELL", ""))
    if shell not in SHELLS:
        raise CliFailure(
            "unsupported_shell",
            f"Unsupported or unknown shell '{shell or '?'}'; pass one of {', '.join(sorted(SHELLS))}",
            EXIT_CONFIGURATION_ERROR,
        )

    if not install:
        click.echo(completion_script(shell))
        return

    changed, rc_path = install_completion(shell)
    shown = escape(str(rc_path))  # a `[` in $HOME is not Rich markup
    if changed:
        ui.success(f"Added lium completion to {shown}")
        ui.info(f"Restart the shell or run: source {shown}")
    else:
        ui.info(f"lium completion is already in {shown}")
