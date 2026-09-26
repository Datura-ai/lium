"""``lium provider portal {login,logout,whoami}`` -- portal session management."""

from __future__ import annotations

import os

import click

from lium.cli.interactive import is_interactive
from lium.cli.provider._client import build_client
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import (
    discord_incentive_warnings,
    emit_error,
    fatal,
    render,
)
from lium.cli.settings import ConfigManager
from lium.provider.client import ProviderClient, discord_connected_from_profile
from lium.provider.errors import ARG_INVALID, INPUT_REQUIRED, ProviderError

PASSWORD_ENV = "LIUM_PROVIDER_PASSWORD"


@click.group("portal")
def portal_command() -> None:
    """Manage the lium-miner-portal JWT session for the configured hotkey."""


@portal_command.command("login", short_help="Exchange a hotkey signature (or e-mail and password) for a JWT.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the local token cache and re-authenticate.",
)
@click.option(
    "--email",
    default=None,
    help=f"Sign in to an account created with e-mail and password (no key needed); the password comes from {PASSWORD_ENV}.",
)
@with_provider_overrides
@click.pass_context
def login(ctx: click.Context, force: bool, email: str | None) -> None:
    """Sign in with the hotkey's signature, or with --email and the password in LIUM_PROVIDER_PASSWORD.

    An e-mail session is stored for later commands (`provider.email` in ~/.lium/config.ini); without a
    terminal or under --json a missing LIUM_PROVIDER_PASSWORD is `input.input_required` (exit 2).
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if email:
        _login_email(ctx, email.strip())
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
    opts = (ctx.obj or {}).get("provider_opts") or {}
    password = os.environ.get(PASSWORD_ENV) or ""
    if not password:
        if opts.get("json") or not is_interactive():
            fatal(
                ctx,
                ProviderError(
                    f"portal login --email needs the password in {PASSWORD_ENV}",
                    code=INPUT_REQUIRED,
                    hint=f"Set {PASSWORD_ENV} and re-run; no prompt is shown without a terminal or under --json.",
                    context={"env": PASSWORD_ENV},
                ),
            )
            return
        try:
            password = click.prompt("Password", hide_input=True, err=True)
        except click.Abort:
            fatal(
                ctx,
                ProviderError(
                    "no password was typed",
                    code=INPUT_REQUIRED,
                    hint=f"Set {PASSWORD_ENV} and re-run.",
                    context={"env": PASSWORD_ENV},
                ),
            )
            return
    client = ProviderClient.signed_out(portal_url=opts.get("portal_url"))
    try:
        body = client.login_email(email, password)
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    ConfigManager().set("provider.email", email)
    miner = body.get("miner") if isinstance(body.get("miner"), dict) else {}
    render(
        ctx,
        {
            "email": email,
            "provider_id": miner.get("id"),
            "hotkey": miner.get("miner_hotkey"),
            "token_present": True,
        },
        summary=f"logged in as {email} (provider_id={miner.get('id')})",
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
