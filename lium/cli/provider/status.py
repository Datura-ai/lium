"""``lium provider status`` -- aggregated provider snapshot.

Composes registration (subtensor metagraph), portal liveness (``/auth/me``),
executor list, and validator weights into a single :class:`ProviderStatus`.
Sources that fail are skipped; their failure is appended to ``warnings``.
"""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import emit_error, render
from lium.provider.errors import ARG_INVALID, ProviderError


@click.command("status", short_help="Aggregated provider snapshot.")
@click.option(
    "--netuid",
    type=int,
    default=51,
    show_default=True,
    help="Subnet to query for registration + validator weights.",
)
@with_provider_overrides
@click.pass_context
def status_command(ctx: click.Context, netuid: int) -> None:
    """Show a snapshot of registration, portal session, and node count."""
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if not opts.get("hotkey"):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "status requires --hotkey (or LIUM_PROVIDER_HOTKEY, or `provider.hotkey` in ~/.lium/config.ini)",
                    code=ARG_INVALID,
                ),
            )
        )
        return

    client = build_client(ctx)
    try:
        snapshot = client.status(netuid=netuid)
    except ProviderError as e:
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
    if snapshot.discord_connected is not None:
        summary_parts.append(f"discord_connected={snapshot.discord_connected}")
    if snapshot.extra_incentive_eligible is not None:
        summary_parts.append(
            f"extra_incentive_eligible={snapshot.extra_incentive_eligible}"
        )
    summary_parts.append(f"nodes={snapshot.node_count or 0}")
    summary_parts.append(f"weights={len(snapshot.validator_weights)}")
    summary = "provider status: " + ", ".join(summary_parts)
    render(ctx, snapshot, summary=summary)


__all__ = ["status_command"]
