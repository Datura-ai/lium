"""Schedules command group."""

import click
from .list.command import schedules_list_command
from .rm.command import schedules_rm_command


@click.group(invoke_without_command=True)
@click.pass_context
def schedules_command(ctx):
    """Manage scheduled pod terminations."""
    # If no subcommand is provided, default to list
    if ctx.invoked_subcommand is None:
        ctx.invoke(schedules_list_command)


# Add subcommands to the schedules group
schedules_command.add_command(schedules_list_command)
schedules_command.add_command(schedules_rm_command)

__all__ = ["schedules_command"]
