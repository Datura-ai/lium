"""`lium env` command group — manage multiple API environments (prod/staging/...)."""

import click

from .use import env_use_command
from .show import env_show_command
from .list_cmd import env_list_command
from .set_cmd import env_set_command
from .remove import env_remove_command


@click.group(name="env")
def env_command():
    """Manage Lium environments (prod / staging / custom).

    Each environment stores its own API key and base URLs in
    ~/.lium/config.ini under section [env.<name>]. The active
    environment is selected via `lium env use <name>` or the
    LIUM_ENV environment variable.
    """
    pass


env_command.add_command(env_use_command)
env_command.add_command(env_show_command)
env_command.add_command(env_list_command)
env_command.add_command(env_set_command)
env_command.add_command(env_remove_command)


__all__ = ["env_command"]
