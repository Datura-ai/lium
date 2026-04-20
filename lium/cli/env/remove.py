"""`lium env remove` — delete a stored environment."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors

from .helpers import (
    DEFAULT_ENV,
    active_env,
    env_section,
    read_config,
    save_config,
    set_active_env,
)


@click.command(name="remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
@handle_errors
def env_remove_command(name, yes):
    """Remove a stored environment section from config.ini.

    The preset name itself (prod, staging) remains available with
    built-in defaults; only the stored api_key/URLs are dropped.
    """
    cfg = read_config()
    section = env_section(name)
    if not cfg.has_section(section):
        ui.warning(f"No stored config for env '{name}'.")
        return

    if not yes and not ui.confirm(
        f"Remove stored config for env '{name}'?", default=False
    ):
        ui.dim("Aborted.")
        return

    cfg.remove_section(section)

    # If we just removed the active env, fall back to prod.
    if active_env() == name:
        set_active_env(cfg, DEFAULT_ENV)
        ui.warning(f"Active env was '{name}', switched back to '{DEFAULT_ENV}'.")

    save_config(cfg)
    ui.success(f"Removed env '{name}'.")
