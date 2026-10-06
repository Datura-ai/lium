"""``lium provider portal {login,logout,whoami}`` -- portal session management."""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import (
    discord_incentive_warnings,
    emit_error,
    render,
)
from lium.provider.client import discord_connected_from_profile
from lium.provider.errors import ARG_INVALID, ProviderError


@click.group("portal")
def portal_command() -> None:
    """Manage the lium-miner-portal JWT session for the configured hotkey."""


@portal_command.command("login", short_help="Exchange a hotkey signature for a JWT.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the local token cache and re-authenticate.",
)
@with_provider_overrides
@click.pass_context
def login(ctx: click.Context, force: bool) -> None:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if not opts.get("hotkey"):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "portal login requires --hotkey (or LIUM_PROVIDER_HOTKEY)",
                    code=ARG_INVALID,
                ),
            )
        )

    client = build_client(ctx)
    try:
        response = client.login(force=force)
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return

    discord_connected: bool | None = None
    try:
        discord_connected = discord_connected_from_profile(client.whoami())
    except ProviderError:
        if response.provider.discord_id is not None:
            discord_connected = bool(response.provider.discord_id)

    summary = f"logged in as provider_id={response.provider.id} (hotkey {response.provider.miner_hotkey})"
    render(
        ctx,
        {
            "provider_id": response.provider.id,
            "hotkey": response.provider.miner_hotkey,
            "token_present": bool(response.token),
            "discord_connected": discord_connected,
            "extra_incentive_eligible": discord_connected
            if discord_connected is not None
            else None,
        },
        summary=summary,
        warnings=discord_incentive_warnings(discord_connected),
    )


@portal_command.command("logout", short_help="Drop the cached JWT for this hotkey.")
@with_provider_overrides
@click.pass_context
def logout(ctx: click.Context) -> None:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if not opts.get("hotkey"):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "portal logout requires --hotkey (or LIUM_PROVIDER_HOTKEY)",
                    code=ARG_INVALID,
                ),
            )
        )

    client = build_client(ctx)
    # Resolve ss58 before clearing so logout output matches the format used by
    # `login` (ss58 hotkey address rather than the bittensor wallet name).
    ss58: str | None
    try:
        ss58 = client.signer.ss58_address
    except Exception:
        ss58 = None
    try:
        client.logout()
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    render(
        ctx,
        {"hotkey": ss58 or opts.get("hotkey")},
        summary="logged out (cache cleared)",
    )


@portal_command.command("whoami", short_help="Call /auth/me with the cached token.")
@with_provider_overrides
@click.pass_context
def whoami(ctx: click.Context) -> None:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if not opts.get("hotkey"):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "portal whoami requires --hotkey (or LIUM_PROVIDER_HOTKEY)",
                    code=ARG_INVALID,
                ),
            )
        )

    client = build_client(ctx)
    try:
        body = client.whoami()
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    summary = "portal session active"
    render(ctx, body, summary=summary)


__all__ = ["portal_command"]
