"""Mine command group."""

from __future__ import annotations

import click

from .executor_setup import executor_setup_command
from .gpu_splitting import gpu_splitting_command


@click.group(name="mine", invoke_without_command=True)
@click.pass_context
def mine_command(ctx: click.Context):
    """Mining-related commands."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


mine_command.add_command(executor_setup_command)
mine_command.add_command(gpu_splitting_command)
