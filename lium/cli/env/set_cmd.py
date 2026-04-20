"""`lium env set` — create/update an environment."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors

from .helpers import env_section, read_config, save_config


@click.command(name="set")
@click.argument("name")
@click.option("--api-key", "api_key", default=None, help="API key for this environment.")
@click.option("--base-url", "base_url", default=None, help="Override base API URL.")
@click.option(
    "--pay-url",
    "base_pay_url",
    default=None,
    help="Override pay-api base URL.",
)
@click.option(
    "--api-key-stdin",
    is_flag=True,
    default=False,
    help="Read API key from stdin (use with prompt tooling).",
)
@handle_errors
def env_set_command(name, api_key, base_url, base_pay_url, api_key_stdin):
    """Create or update an environment's settings.

    Example:
      lium env set staging --api-key sk_... --base-url https://staging.lium.io/api
      lium env set prod --api-key sk_...
    """
    if api_key_stdin and not api_key:
        api_key = ui.prompt(f"Enter API key for '{name}'") or None

    cfg = read_config()
    section = env_section(name)
    if not cfg.has_section(section):
        cfg.add_section(section)

    changed = []
    if api_key is not None:
        cfg.set(section, "api_key", api_key)
        changed.append("api_key")
    if base_url is not None:
        cfg.set(section, "base_url", base_url)
        changed.append("base_url")
    if base_pay_url is not None:
        cfg.set(section, "base_pay_url", base_pay_url)
        changed.append("base_pay_url")

    if not changed:
        ui.warning(
            "Nothing to set. Pass --api-key / --base-url / --pay-url "
            "(at least one)."
        )
        return

    save_config(cfg)
    ui.success(f"Updated env '{name}': {', '.join(changed)}")
    ui.dim("Tip: activate it with `lium env use " + name + "`")
