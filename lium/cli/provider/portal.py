"""``lium provider portal {login,confirm-email,logout,whoami}`` -- portal session management."""

from __future__ import annotations

import os

import click

from lium.cli.commands.mine_register import portal_web_url
from lium.cli.interactive import is_interactive
from lium.cli.provider._client import (
    AUTH_EMAIL_SESSION,
    AUTH_HOTKEY,
    AUTH_TOKEN,
    PROVIDER_TOKEN_ENV,
    auth_method,
    build_client,
)
from lium.cli.provider._guards import not_signed_in_error, require_hotkey
from lium.cli.provider._handoff import run_handoff
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

    Google sign-in is for people in the portal. An agent does not use it: a person signed in creates a
    token with `lium provider token create`, and the agent sets LIUM_PROVIDER_TOKEN to it.
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


@portal_command.command("confirm-email", short_help="Confirm the account's e-mail (a one-time human step).")
@click.option(
    "--wait",
    is_flag=True,
    help="After the handoff is printed, poll until the e-mail is confirmed (exit 0) or the code expires "
    "(human.handoff_expired, exit 12).",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=0),
    default=None,
    help="Seconds to wait with --wait (default: until the code expires).",
)
@click.option(
    "--poll-interval",
    type=click.FloatRange(min=0.1),
    default=3.0,
    show_default=True,
    help="Seconds between status checks while waiting.",
)
@with_provider_overrides
@click.pass_context
def confirm_email(
    ctx: click.Context, wait: bool, timeout: int | None, poll_interval: float
) -> None:
    """Confirming the e-mail is a one-time human step.

    The portal hands out one URL plus a short code: without --wait this is human.handoff_required
    (exit 12) with data {step, handoff_url, code, expires_at, message_for_human}; relay
    message_for_human to the person, who enters the code in the portal and follows the mailed link.
    A portal without handoff sessions answers portal.not_supported (exit 3);
    data.legacy_browser_url is the portal page the old flow uses.
    """
    require_hotkey(ctx, group="portal")
    opts = (ctx.obj or {}).get("provider_opts") or {}
    client = build_client(ctx)
    try:
        result = run_handoff(
            client,
            step="email_confirm",
            wait=wait,
            timeout=timeout,
            poll_interval=poll_interval,
            json_mode=bool(opts.get("json")),
            legacy_url=lambda: _email_legacy_url(opts),
        )
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    render(ctx, result, summary="e-mail confirmed")


def _email_legacy_url(opts) -> str:
    """The portal page of the old flow: the confirmation link in the mail lands the person there."""
    return f"{portal_web_url(opts.get('portal_url'))}/settings"


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


_AUTH_LABELS = {
    AUTH_TOKEN: f"API token ({PROVIDER_TOKEN_ENV})",
    AUTH_HOTKEY: "hotkey",
    AUTH_EMAIL_SESSION: "e-mail session",
}


@portal_command.command("whoami", short_help="Call /auth/me with the cached token.")
@with_provider_overrides
@click.pass_context
def whoami(ctx: click.Context) -> None:
    """Show the signed-in account and how this command signed in.

    Any sign-in works: LIUM_PROVIDER_TOKEN (a provider API token), the hotkey, or the session of
    `portal login --email`, first match in that order. `auth_method` (token, hotkey, email_session) is in the
    --json output always, and in the text output unless the hotkey signed in (then the text is as before).
    Signed in nowhere: `auth.not_signed_in` (exit 6 under --json; ARG_INVALID, exit 1, in text mode).
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    method = auth_method(opts)
    if method is None:
        fatal(ctx, not_signed_in_error())
        return
    client = build_client(ctx)
    try:
        body = client.whoami()
    except ProviderError as e:
        ctx.exit(emit_error(ctx, e))
        return
    if method == AUTH_HOTKEY and not opts.get("json"):
        render(ctx, body, summary="portal session active")
        return
    render(
        ctx,
        {**(body if isinstance(body, dict) else {}), "auth_method": method},
        summary=f"portal session active, signed in by {_AUTH_LABELS[method]}",
    )


__all__ = ["portal_command"]
