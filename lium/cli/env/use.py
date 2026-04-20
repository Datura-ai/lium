"""`lium env use` — switch active environment."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors

from .helpers import ENV_PRESETS, env_section, read_config, save_config, set_active_env


@click.command(name="use")
@click.argument("name")
@handle_errors
def env_use_command(name: str):
    """Switch the active environment.

    Example:
      lium env use staging
      lium env use prod
    """
    cfg = read_config()
    is_preset = name in ENV_PRESETS
    has_section = cfg.has_section(env_section(name))

    if not is_preset and not has_section:
        ui.error(
            f"Unknown environment '{name}'. "
            f"Define it first: lium env set {name} --api-key <KEY> [--base-url URL]"
        )
        return

    set_active_env(cfg, name)
    save_config(cfg)
    ui.success(f"Active environment → {name}")

    # Warn when the env has no api_key yet.
    api_key_present = has_section and cfg.get(
        env_section(name), "api_key", fallback=""
    )
    if not api_key_present:
        ui.warning(
            f"No API key stored for '{name}'. Set one with: "
            f"lium env set {name} --api-key <KEY>"
        )
