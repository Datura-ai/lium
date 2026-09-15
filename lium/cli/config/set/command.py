"""Config set command implementation."""

from typing import Optional

import click

from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, EXIT_GENERAL_ERROR, handle_errors
from . import validation
from .actions import SetConfigAction


def mask_value(value: str, key: str) -> str:
    """Mask sensitive values (API keys, the `[session] token` from `lium workspaces login`)."""
    if (key.endswith('api_key') or key == 'session.token') and value:
        return value[:8] + '...' + value[-4:] if len(value) > 12 else '***'
    return value


@click.command(name="set")
@click.argument("key")
@click.argument("value", required=False)
@handle_errors
def config_set_command(key: str, value: Optional[str]):
    """Set a configuration value. Run without value for interactive mode."""

    # Validate
    valid, error = validation.validate(key)
    if not valid:
        raise CliFailure("invalid_key", error, EXIT_CONFIGURATION_ERROR)

    # Execute
    ctx = {"key": key, "value": value or ""}

    action = SetConfigAction()
    result = action.execute(ctx)

    if not result.ok:
        raise CliFailure("config_set_failed", result.error, EXIT_GENERAL_ERROR)
