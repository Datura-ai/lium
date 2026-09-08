"""Volumes command group."""

import click

from .list.command import volumes_list_command
from .new.command import volumes_new_command
from .rm.command import volumes_rm_command


@click.group(invoke_without_command=True)
@click.pass_context
def volumes_command(ctx):
    """Manage persistent volumes.

    \b
    Examples:
      lium volumes                        # same as 'lium volumes list'
      lium volumes new datasets -d "training data"
      lium volumes rm 1 --yes             # index from 'lium volumes'
      lium up --gpu H100 -v id:<VOLUME_HUID>
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(volumes_list_command)


volumes_command.add_command(volumes_list_command)
volumes_command.add_command(volumes_new_command)
volumes_command.add_command(volumes_rm_command)

__all__ = ["volumes_command"]
