"""Config get command implementation."""

import click

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR, handle_errors
from . import validation
from .actions import GetConfigAction


def mask_value(value: str, key: str) -> str:
    """Mask sensitive values (API keys, the `[session] token` from `lium workspaces login`)."""
    if (key.endswith('api_key') or key == 'session.token') and value:
        return value[:8] + '...' + value[-4:] if len(value) > 12 else '***'
    return value


@click.command(name="get")
@click.argument("key")
@handle_errors
def config_get_command(key: str):
    """Get a configuration value."""

    # Validate
    valid, error = validation.validate(key)
    if not valid:
        raise CliFailure("invalid_key", error, EXIT_CONFIGURATION_ERROR)

    # Execute
    ctx = {"key": key}

    action = GetConfigAction()
    result = action.execute(ctx)

    if not result.ok:
        raise CliFailure("key_not_found", result.error, EXIT_GENERAL_ERROR)

    value = result.data.get("value")
    styled_value = mask_value(value, key)
    ui.info(styled_value)
    source = result.data.get("source")
    if source:
        ui.dim(f"from {source}")
