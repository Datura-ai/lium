"""``lium provider portal {login,logout,whoami}`` -- portal session management."""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._render import emit_error, render
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

    summary = f"logged in as provider_id={response.provider.id} (hotkey {response.provider.miner_hotkey})"
    render(
        ctx,
        {
            "provider_id": response.provider.id,
            "hotkey": response.provider.miner_hotkey,
            "token_present": bool(response.token),
        },
        summary=summary,
    )


@portal_command.command("logout", short_help="Drop the cached JWT for this hotkey.")
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
