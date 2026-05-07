"""``lium miner status`` -- aggregated provider snapshot.

Composes registration (subtensor metagraph), portal liveness (``/auth/me``),
executor list, and validator weights into a single :class:`MinerStatus`.
Sources that fail are skipped; their failure is appended to ``warnings``.
"""

from __future__ import annotations

import click

from lium.cli.miner._client import build_client
from lium.cli.miner._render import emit_error, render
from lium.miner.errors import ARG_INVALID, MinerError


@click.command("status", short_help="Aggregated provider snapshot.")
@click.option(
    "--netuid",
    type=int,
    default=51,
    show_default=True,
    help="Subnet to query for registration + validator weights.",
)
@click.pass_context
def status_command(ctx: click.Context, netuid: int) -> None:
    """Show a snapshot of registration, portal session, and executor count."""
    opts = (ctx.obj or {}).get("miner_opts") or {}
    if not opts.get("hotkey"):
        ctx.exit(
            emit_error(
                ctx,
                MinerError(
                    "status requires --hotkey (or LIUM_MINER_HOTKEY, or `miner.hotkey` in ~/.lium/config.ini)",
                    code=ARG_INVALID,
                ),
            )
        )
        return

    client = build_client(ctx)
    try:
        snapshot = client.status(netuid=netuid)
    except MinerError as e:
        ctx.exit(emit_error(ctx, e))
        return

    summary_parts: list[str] = []
    if snapshot.hotkey:
        summary_parts.append(f"hotkey={snapshot.hotkey}")
    summary_parts.append(
        f"registered={snapshot.registered_on_subnet}"
        if snapshot.registered_on_subnet is not None
        else "registered=unknown"
    )
    summary_parts.append(f"portal={'active' if snapshot.portal_session_active else 'down'}")
    summary_parts.append(f"executors={snapshot.executor_count or 0}")
    summary_parts.append(f"weights={len(snapshot.validator_weights)}")
    summary = "miner status: " + ", ".join(summary_parts)
    render(ctx, snapshot, summary=summary)


__all__ = ["status_command"]
