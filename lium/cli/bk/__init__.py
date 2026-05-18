"""Backup command group."""

import click

from .show.command import bk_show_command
from .rm.command import bk_rm_command
from .set.command import bk_set_command
from .now.command import bk_now_command
from .logs.command import bk_logs_command
from .restore_logs.command import bk_restore_logs_command
from .restore.command import bk_restore_command


@click.group()
def bk_command():
    """Manage pod backup configurations."""
    pass


bk_command.add_command(bk_show_command)
bk_command.add_command(bk_rm_command)
bk_command.add_command(bk_set_command)
bk_command.add_command(bk_now_command)
bk_command.add_command(bk_logs_command)
bk_command.add_command(bk_restore_logs_command)
bk_command.add_command(bk_restore_command)

__all__ = ["bk_command"]
