"""Volumes rm command."""

from datetime import datetime, timezone

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    handle_errors,
    ensure_config,
    get_last_volume_selection,
    POD_INDEX_TTL_SECONDS,
    _snapshot_age_seconds,
)
from . import validation, parsing
from .actions import RemoveVolumesAction


@click.command("rm")
@click.argument("indices")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def volumes_rm_command(indices: str, yes: bool):
    """Remove volumes by index from last 'lium volumes' list."""
    ensure_config()

    # Validate
    valid, error = validation.validate(indices)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Get cached volumes
    last_selection = get_last_volume_selection()
    if not last_selection or not last_selection.get('volumes', []):
        raise CliFailure(
            "no_volumes_cached",
            "No volumes cached. Run 'lium volumes' first.",
            EXIT_GENERAL_ERROR,
        )

    age = _snapshot_age_seconds(last_selection, datetime.now(timezone.utc))
    if age is None or age < 0 or age > POD_INDEX_TTL_SECONDS:
        # same rule as pod indexes: never delete by a row number the caller saw too long ago
        raise CliFailure(
            "stale_volume_index",
            f"The last 'lium volumes' list is older than {POD_INDEX_TTL_SECONDS // 60} minutes. "
            "Run 'lium volumes' and retry.",
            EXIT_CONFIGURATION_ERROR,
        )

    volumes_data = last_selection.get('volumes', [])

    # Parse
    parsed, error = parsing.parse(indices, volumes_data)
    if error:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    volumes_to_remove = parsed["volumes_to_remove"]

    # Confirm
    if not yes:
        count = len(volumes_to_remove)
        names = ", ".join(f"{v['huid']} ({v.get('name') or '-'})" for _, v in volumes_to_remove)
        message = f"Remove {count} volume{'s' if count > 1 else ''}: {names}?"
        if not ui.confirm(message):
            return

    # Execute
    lium = Lium()
    ctx = {"lium": lium, "volumes_to_remove": volumes_to_remove, "ui": ui}

    action = RemoveVolumesAction()
    result = action.execute(ctx)

    if not result.ok:
        failures = result.data.get("failures", [])
        raise CliFailure(
            "volume_removal_failed",
            f"Failed to remove volumes: {', '.join(failures)}",
            EXIT_GENERAL_ERROR,
        )
    ui.success(f"Removed {', '.join(v['huid'] for _, v in volumes_to_remove)}")
