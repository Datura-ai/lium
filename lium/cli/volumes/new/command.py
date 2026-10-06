"""Volumes new command."""

from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from .actions import CreateVolumeAction


@click.command("new")
@click.argument("name")
@click.option("--description", "--desc", "-d", "desc", help="Volume description")
@handle_errors
def volumes_new_command(name: str, desc: Optional[str]):
    """Create a new volume."""
    ensure_config()

    lium = Lium()
    ctx = {"lium": lium, "name": name, "description": desc or ""}

    action = CreateVolumeAction()
    ui.load(f"Creating volume '{name}'", lambda: action.execute(ctx))
