"""Config command group."""

import click

from .get.command import config_get_command
from .set.command import config_set_command
from .unset.command import config_unset_command
from .show.command import config_show_command
from .path.command import config_path_command
from .edit.command import config_edit_command
from .reset.command import config_reset_command


@click.group(name="config")
def config_command():
    """Manage Lium CLI configuration."""
    pass


config_command.add_command(config_get_command)
config_command.add_command(config_set_command)
config_command.add_command(config_unset_command)
config_command.add_command(config_show_command)
config_command.add_command(config_path_command)
config_command.add_command(config_edit_command)
config_command.add_command(config_reset_command)

__all__ = ["config_command"]
