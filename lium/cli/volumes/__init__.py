"""Volumes command group."""

import click

from .list.command import volumes_list_command
from .new.command import volumes_new_command
from .rm.command import volumes_rm_command


@click.group(invoke_without_command=True)
@click.pass_context
def volumes_command(ctx):
    """Manage persistent volumes."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(volumes_list_command)


volumes_command.add_command(volumes_list_command)
volumes_command.add_command(volumes_new_command)
volumes_command.add_command(volumes_rm_command)

__all__ = ["volumes_command"]
