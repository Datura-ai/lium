"""SSH keys command group."""

import click

from .list.command import ssh_keys_list_command
from .sync.command import ssh_keys_sync_command


@click.group(invoke_without_command=True)
@click.pass_context
def ssh_keys_command(ctx):
    """Manage SSH public keys registered with Lium."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(ssh_keys_list_command)


ssh_keys_command.add_command(ssh_keys_list_command)
ssh_keys_command.add_command(ssh_keys_sync_command)

__all__ = ["ssh_keys_command"]
