"""SSH keys command group."""

import click

from .list.command import ssh_keys_list_command
from .sync.command import ssh_keys_sync_command


@click.group(invoke_without_command=True)
@click.pass_context
def ssh_keys_command(ctx):
    """Manage SSH public keys registered with Lium.

    \b
    Examples:
      lium ssh-keys                # same as 'lium ssh-keys list'
      lium ssh-keys sync           # register the local public key
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(ssh_keys_list_command)


ssh_keys_command.add_command(ssh_keys_list_command)
ssh_keys_command.add_command(ssh_keys_sync_command)

__all__ = ["ssh_keys_command"]
