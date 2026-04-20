"""`lium env list` — list known environments."""

import click

from lium.cli import ui
from lium.cli.utils import handle_errors

from .helpers import ENV_PRESETS, active_env, env_values, known_envs, mask_key, read_config


@click.command(name="list")
@handle_errors
def env_list_command():
    """List all known environments and mark the active one."""
    cfg = read_config()
    current = active_env()

    rows = []
    for env in known_envs():
        vals = env_values(cfg, env)
        is_preset = env in ENV_PRESETS
        marker = "*" if env == current else ""
        rows.append(
            [
                marker,
                env,
                "preset" if is_preset else "custom",
                vals.get("base_url", "-"),
                mask_key(vals.get("api_key", "")) or "—",
            ]
        )

    ui.table(
        ["", "Env", "Type", "Base URL", "API key"],
        rows,
    )
    ui.dim(f"Active: {current}  (override with LIUM_ENV or `lium env use <name>`)")
