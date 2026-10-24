"""``lium provider portal {login,logout,whoami}`` -- portal session management."""

from __future__ import annotations

import os

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import (
    discord_incentive_warnings,
    emit_error,
    render,
)
from lium.cli.settings import ConfigManager
from lium.provider.client import ProviderClient, discord_connected_from_profile
from lium.provider.errors import ARG_INVALID, PORTAL_AUTH_INVALID, ProviderError


@click.group("portal")
def portal_command() -> None:
    """Manage the lium-miner-portal JWT session for the configured hotkey."""


@portal_command.command("login", short_help="Exchange a hotkey signature for a JWT.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the local token cache and re-authenticate.",
)
@click.option(
    "--email",
    default=None,
    help="Sign in to an account made with e-mail (no key). The password is read from "
    "LIUM_PROVIDER_PASSWORD or asked for.",
)
@with_provider_overrides
@click.pass_context
def login(ctx: click.Context, force: bool, email: str | None) -> None:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if email:
        _login_email(ctx, email)
        return
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


def _login_email(ctx: click.Context, email: str) -> None:
    password = os.environ.get("LIUM_PROVIDER_PASSWORD") or click.prompt("Password", hide_input=True)
    opts = (ctx.obj or {}).get("provider_opts") or {}
    # the account id is only known after sign-in; the e-mail is a placeholder until then
    client = ProviderClient(portal_url=opts.get("portal_url"), account=email)
    try:
        response = client.login_email(email, password)
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    # later provider commands without --hotkey use this account's cached session
    ConfigManager().set("provider.account", response.provider.miner_hotkey)
    render(
        ctx,
        {"provider_id": response.provider.id, "account": response.provider.miner_hotkey, "token_present": True},
        summary=f"logged in as provider_id={response.provider.id} ({email})",
    )


@portal_command.command("logout", short_help="Drop the cached JWT for this hotkey.")
@with_provider_overrides
@click.pass_context
def logout(ctx: click.Context) -> None:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if not (opts.get("hotkey") or opts.get("account")):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "portal logout requires --hotkey (or LIUM_PROVIDER_HOTKEY), or `portal login --email`",
                    code=ARG_INVALID,
                ),
            )
        )

    client = build_client(ctx)
    # Resolve ss58 before clearing so logout output matches the format used by
    # `login` (ss58 hotkey address rather than the bittensor wallet name).
    ss58: str | None
    try:
        ss58 = client.account or client.signer.ss58_address
    except Exception:
        ss58 = None
    try:
        client.logout()
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    if client.account:
        ConfigManager().unset("provider.account")
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
    if not (opts.get("hotkey") or opts.get("account")):
        ctx.exit(
            emit_error(
                ctx,
                ProviderError(
                    "portal whoami requires --hotkey (or LIUM_PROVIDER_HOTKEY), or `portal login --email`",
                    code=ARG_INVALID,
                ),
            )
        )

    client = build_client(ctx)
    try:
        body = client.whoami()
    except ProviderError as e:
        if e.code == PORTAL_AUTH_INVALID and client._current_token() is None:
            # nothing was sent to reject: say so instead of "token rejected"
            e = ProviderError(
                "no portal session for this hotkey",
                code=PORTAL_AUTH_INVALID,
                hint="Log in with `lium provider portal login`.",
            )
        ctx.exit(emit_error(ctx, e))
        return
    summary = "portal session active"
    render(ctx, body, summary=summary)


__all__ = ["portal_command"]
