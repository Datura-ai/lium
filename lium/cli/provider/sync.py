"""``lium provider sync …`` -- batch node sync between portal & central miner.

Two CLI verbs match the user-facing button labels in the portal frontend:

- ``from-miner-server`` -- pull node state from the central miner server.
- ``to-miner-server``   -- push node state to the central miner server.

The HTTP routes the portal exposes are still named ``/executors/sync-
executor-{central-miner,miner-portal}``. The SDK methods on
``ProviderClient`` (``sync_nodes_from_miner_server`` /
``sync_nodes_to_miner_server``) take the user-facing names and wrap the
underlying routes per the table at the top of ``client.py``.
"""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import (
    handle_provider_error,
    require_hotkey,
    require_persona_ack,
)
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import render
from lium.provider.errors import ProviderError


@click.group("sync")
def sync_command() -> None:
    """Sync nodes between the portal and the central miner server."""


@sync_command.command(
    "from-miner-server",
    short_help="Pull node state from the central miner server (frontend's 'Sync From Miner Server').",
)
@with_provider_overrides
@click.pass_context
def from_miner_server(ctx: click.Context) -> None:
    require_hotkey(ctx, group="sync")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.sync_nodes_from_miner_server()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary="sync from miner server: queued")


@sync_command.command(
    "to-miner-server",
    short_help="Push node state to the central miner server (frontend's 'Sync Into Miner Server').",
)
@with_provider_overrides
@click.pass_context
def to_miner_server(ctx: click.Context) -> None:
    require_hotkey(ctx, group="sync")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.sync_nodes_to_miner_server()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary="sync to miner server: queued")


__all__ = ["sync_command"]
