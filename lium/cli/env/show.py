"""`lium env show` — show details of the active (or named) environment."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors

from .helpers import active_env, env_values, mask_key, read_config


@click.command(name="show")
@click.argument("env", required=False)
@handle_errors
def env_show_command(env):
    """Show details for the active environment (or a named one).

    Example:
      lium env show
      lium env show staging
    """
    cfg = read_config()
    name = env or active_env()
    vals = env_values(cfg, name)

    ui.info(f"Environment: {name}" + (" (active)" if name == active_env() else ""))
    ui.print(f"  base_url     = {vals.get('base_url', '—')}")
    ui.print(f"  base_pay_url = {vals.get('base_pay_url', '—')}")
    api_key = vals.get("api_key", "")
    ui.print(f"  api_key      = {mask_key(api_key) if api_key else '—'}")
