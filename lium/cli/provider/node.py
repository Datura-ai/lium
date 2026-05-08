"""``lium provider node …`` -- node lifecycle on the portal.

Subcommands:

- ``list``                    -- paginated node listing.
- ``get <id>``                -- single node record.
- ``add``                     -- queue a new node (calls /executors).
- ``rm <id>``                 -- delete a node.
- ``update-price <id>``       -- set price-per-GPU.
- ``update-gpu <id>``         -- change GPU type/count.
- ``min-gpu set/unset <id>``  -- min GPU count for rental.
- ``pods <id>``               -- rented pods on the node.
- ``machine-requests <id>``   -- pending tenant requests on this node.
- ``notice-period set/unset`` -- create/delete a notice period.
- ``notify-added <id>``       -- post /machine-added for a request.

Mutating commands (add/rm/update-*/min-gpu set-unset/notice-period
set-unset/notify-added) call the persona gate. ``--yes`` and
``LIUM_PROVIDER_ACK=1`` short-circuit the prompt.

Note: the HTTP routes the portal exposes still spell this concept
``/executors/...`` -- the CLI verb and SDK method names use ``node``
because that's the user-facing terminology.
"""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import (
    handle_provider_error,
    require_hotkey,
    require_persona_ack,
)
from lium.cli.provider._render import render
from lium.provider._shared_config import default_price_for_gpu, fetch_shared_config
from lium.provider.errors import ProviderError


@click.group("node")
def node_command() -> None:
    """Manage GPU nodes registered with the portal."""


@node_command.command("list", short_help="List nodes for this provider.")
@click.option("--miner-hotkey", "miner_hotkey", help="Filter by miner hotkey.")
@click.option("--page", type=int, default=None, help="1-indexed page number.")
@click.option("--limit", type=int, default=None, help="Page size.")
@click.pass_context
def list_nodes(
    ctx: click.Context,
    miner_hotkey: str | None,
    page: int | None,
    limit: int | None,
) -> None:
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        body = client.list_nodes(miner_hotkey=miner_hotkey, page=page, limit=limit)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    rows = body.get("data") if isinstance(body, dict) else body
    total = body.get("total") if isinstance(body, dict) else None
    summary_parts = [f"nodes={len(rows) if isinstance(rows, list) else 0}"]
    if total is not None:
        summary_parts.append(f"total={total}")
    render(ctx, body, summary="node list: " + ", ".join(summary_parts))


@node_command.command("get", short_help="Show one node.")
@click.argument("node_id", required=True)
@click.pass_context
def get_node(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        body = client.get_node(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"node {node_id}")


@node_command.command("add", short_help="Queue a new node addition.")
@click.option("--gpu-type", required=True, help="GPU model (e.g. H100, RTX 4090).")
@click.option("--ip", "ip_address", required=True, help="Node IPv4 address.")
@click.option(
    "--port",
    type=int,
    default=8080,
    show_default=True,
    help="Node port the validator will reach.",
)
@click.option(
    "--price",
    "price_per_gpu",
    type=float,
    default=None,
    help=(
        "USD/GPU/hour. If omitted, the CLI fetches the public shared-config "
        "default for --gpu-type (matches the provider-portal Add-Node modal)."
    ),
)
@click.option(
    "--gpu-count", type=int, default=1, show_default=True, help="Number of GPUs."
)
@click.pass_context
def add_node(
    ctx: click.Context,
    gpu_type: str,
    ip_address: str,
    port: int,
    price_per_gpu: float | None,
    gpu_count: int,
) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    if price_per_gpu is None:
        try:
            snapshot = fetch_shared_config()
            price_per_gpu = default_price_for_gpu(snapshot, gpu_type)
        except ProviderError as e:
            ctx.exit(handle_provider_error(ctx, e))
            return
        click.echo(
            f"Using default price for {gpu_type}: ${price_per_gpu:g}/GPU/hour "
            "(from public shared-config; pass --price to override).",
            err=True,
        )
    client = build_client(ctx)
    try:
        body = client.add_node(
            gpu_type=gpu_type,
            ip_address=ip_address,
            port=port,
            price_per_gpu=price_per_gpu,
            gpu_count=gpu_count,
        )
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(
        ctx,
        body,
        summary=f"queued add node {gpu_count}x{gpu_type} @ {ip_address}:{port}",
    )


@node_command.command("rm", short_help="Delete a node.")
@click.argument("node_id", required=True)
@click.pass_context
def remove_node(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.delete_node(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"deleted node {node_id}")


@node_command.command("update-price", short_help="Set price-per-GPU.")
@click.argument("node_id", required=True)
@click.option("--price", "price_per_gpu", type=float, required=True)
@click.pass_context
def update_price(ctx: click.Context, node_id: str, price_per_gpu: float) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.update_node_price(node_id, price_per_gpu)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"price updated: {node_id} -> ${price_per_gpu}/GPU/hr")


@node_command.command("update-gpu", short_help="Change GPU type/count.")
@click.argument("node_id", required=True)
@click.option("--gpu-type", required=True)
@click.option("--gpu-count", type=int, required=True)
@click.pass_context
def update_gpu(
    ctx: click.Context, node_id: str, gpu_type: str, gpu_count: int
) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.update_node_gpu(node_id, gpu_type=gpu_type, gpu_count=gpu_count)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(
        ctx,
        body,
        summary=f"gpu updated: {node_id} -> {gpu_count}x{gpu_type}",
    )


@node_command.group("min-gpu")
def min_gpu_command() -> None:
    """Min GPU count for rental matchmaking."""


@min_gpu_command.command("set", short_help="Set min GPUs for rental.")
@click.argument("node_id", required=True)
@click.argument("count", type=int, required=True)
@click.pass_context
def set_min_gpu(ctx: click.Context, node_id: str, count: int) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.set_min_gpu_for_rental(node_id, count)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"min-gpu set: {node_id} -> {count}")


@min_gpu_command.command("unset", short_help="Clear min GPUs for rental.")
@click.argument("node_id", required=True)
@click.pass_context
def unset_min_gpu(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.unset_min_gpu_for_rental(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"min-gpu cleared: {node_id}")


@node_command.command("pods", short_help="List pods rented on a node.")
@click.argument("node_id", required=True)
@click.pass_context
def list_pods(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        body = client.node_pods(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    rows = body.get("data") if isinstance(body, dict) else body
    count = len(rows) if isinstance(rows, list) else 0
    render(ctx, body, summary=f"pods on {node_id}: {count}")


@node_command.command("machine-requests", short_help="List pending tenant asks.")
@click.argument("node_id", required=True)
@click.pass_context
def machine_requests(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        body = client.node_machine_requests(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    rows = body.get("data") if isinstance(body, dict) else body
    count = len(rows) if isinstance(rows, list) else 0
    render(ctx, body, summary=f"machine requests for {node_id}: {count}")


@node_command.group("notice-period")
def notice_period_command() -> None:
    """Notice-period scheduling for a node."""


@notice_period_command.command("set", short_help="Open a notice period.")
@click.argument("node_id", required=True)
@click.pass_context
def set_notice_period(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.create_notice_period(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"notice period opened: {node_id}")


@notice_period_command.command("unset", short_help="Cancel the notice period.")
@click.argument("node_id", required=True)
@click.pass_context
def unset_notice_period(ctx: click.Context, node_id: str) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.delete_notice_period(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"notice period cleared: {node_id}")


@node_command.command("notify-added", short_help="Mark a machine request fulfilled.")
@click.argument("node_id", required=True)
@click.option("--request-id", "machine_request_id", required=True)
@click.pass_context
def notify_added(ctx: click.Context, node_id: str, machine_request_id: str) -> None:
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.notify_machine_added(node_id, machine_request_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(
        ctx,
        body,
        summary=f"notified machine added: node={node_id} request={machine_request_id}",
    )


__all__ = ["node_command"]
