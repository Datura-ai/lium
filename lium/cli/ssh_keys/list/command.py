"""`lium ssh-keys list` command."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import ensure_config, handle_errors

from ..display import build_ssh_keys_table
from .actions import GetSSHKeysAction


@click.command("list")
@handle_errors
def ssh_keys_list_command():
    """List SSH keys registered with Lium."""
    ensure_config()

    lium = Lium(source="cli")
    ctx = {"lium": lium}

    action = GetSSHKeysAction()
    result = ui.load("Loading SSH keys", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    keys = result.data["keys"]

    if not keys:
        ui.info("No SSH keys registered yet.")
        ui.dim("Tip: lium up <executor>  # auto-registers your local key")
        return

    table, header = build_ssh_keys_table(keys)
    ui.info(header)
    ui.print(table)
