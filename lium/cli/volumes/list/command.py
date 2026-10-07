"""Volumes list command."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config, store_volume_selection
from ..display import build_volumes_table
from .actions import GetVolumesAction


@click.command("list")
@handle_errors
def volumes_list_command():
    """List all volumes."""
    ensure_config()

    lium = Lium()
    ctx = {"lium": lium}

    action = GetVolumesAction()
    result = ui.load("Loading volumes", lambda: action.execute(ctx))

    volumes = result.data["volumes"]

    if not volumes:
        store_volume_selection([])  # a stale list would let `volumes rm 1` delete a volume not shown
        ui.info("No volumes. Create one with: lium volumes new <name>")
        return

    table, header, tip = build_volumes_table(volumes)

    ui.info(header)
    ui.print(table)
    ui.print("")
    ui.info(tip)

    store_volume_selection(volumes)
