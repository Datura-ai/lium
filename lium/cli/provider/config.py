"""``lium provider config …`` -- portal account & central-miner configuration.

Subcommands:

- ``show``                       -- ``GET /auth/me`` rich profile.
- ``opt-in / opt-out``           -- toggle the lium.io central miner server.
- ``set-email <email>``          -- update contact email.
- ``set-password``               -- set password using signature authentication.
- ``connect-discord``            -- start Discord OAuth linking.
- ``set-subscriptions``          -- machine-request notification subscriptions.

Distinction from ``lium config`` (CLI-side ConfigManager) and
``lium provider portal`` (JWT/session): this group is for *portal-account*
state held server-side. Some account mutations run the persona gate.
"""

from __future__ import annotations

import time
import webbrowser

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import (
    handle_provider_error,
    require_hotkey,
    require_persona_ack,
)
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import discord_incentive_warnings, render
from lium.provider.client import discord_connected_from_profile, with_discord_eligibility
from lium.provider.errors import ARG_INVALID, ProviderError


@click.group("config")
def config_command() -> None:
    """Provider account & central-miner-server configuration."""


@config_command.command("show", short_help="Full portal account profile.")
@with_provider_overrides
@click.pass_context
def show(ctx: click.Context) -> None:
    require_hotkey(ctx, group="config")
    client = build_client(ctx)
    try:
        body = client.whoami()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    if isinstance(body, dict):
        body = with_discord_eligibility(body)
    summary_parts: list[str] = []
    if isinstance(body, dict):
        for key in (
            "miner_hotkey",
            "miner_uid",
            "email",
            "opt_in_status",
            "discord_connected",
        ):
            if key in body:
                summary_parts.append(f"{key}={body[key]}")
    summary = "config: " + ", ".join(summary_parts) if summary_parts else "config"
    warnings = (
        discord_incentive_warnings(body.get("discord_connected"))
        if isinstance(body, dict)
        else []
    )
    render(ctx, body, summary=summary, warnings=warnings)


def _set_opt_in(ctx: click.Context, value: bool) -> None:
    require_hotkey(ctx, group="config")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        response = client.set_opt_in_status(value)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    state = "ON" if value else "OFF"
    summary = (
        f"central miner server: {state} "
        f"(ip={response.central_miner_ip}, port={response.central_miner_port})"
    )
    render(ctx, response, summary=summary)


@config_command.command("opt-in", short_help="Use lium.io's central miner server.")
@with_provider_overrides
@click.pass_context
def opt_in(ctx: click.Context) -> None:
    _set_opt_in(ctx, True)


@config_command.command("opt-out", short_help="Run your own central miner server.")
@with_provider_overrides
@click.pass_context
def opt_out(ctx: click.Context) -> None:
    _set_opt_in(ctx, False)


@config_command.command("set-email", short_help="Update contact email.")
@click.argument("email", required=True)
@with_provider_overrides
@click.pass_context
def set_email(ctx: click.Context, email: str) -> None:
    require_hotkey(ctx, group="config")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.set_email(email)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"email set: {email}")


@config_command.command(
    "set-password",
    short_help="Set the provider portal password with hotkey signature auth.",
)
@click.option(
    "--password",
    "new_password",
    envvar="LIUM_PROVIDER_NEW_PASSWORD",
    help="New password. Prompts when omitted outside --json.",
)
@with_provider_overrides
@click.pass_context
def set_password(ctx: click.Context, new_password: str | None) -> None:
    require_hotkey(ctx, group="config")
    if not new_password:
        if _json_mode(ctx):
            ctx.exit(
                handle_provider_error(
                    ctx,
                    ProviderError(
                        "config set-password requires --password or LIUM_PROVIDER_NEW_PASSWORD under --json",
                        code=ARG_INVALID,
                    ),
                )
            )
            return
        new_password = click.prompt(
            "New password",
            hide_input=True,
            confirmation_prompt=True,
        )

    client = build_client(ctx)
    try:
        body = client.set_password(new_password)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary="password set")


@config_command.command(
    "connect-discord",
    short_help="Start Discord OAuth linking for extra incentive eligibility.",
)
@click.option(
    "--no-wait",
    is_flag=True,
    help="Return immediately after printing the Discord authorization URL.",
)
@click.option(
    "--timeout",
    type=click.IntRange(min=0),
    default=120,
    show_default=True,
    help="Seconds to wait for Discord linking outside --no-wait.",
)
@click.option(
    "--poll-interval",
    type=click.FloatRange(min=0.1),
    default=3.0,
    show_default=True,
    help="Seconds between Discord status checks while waiting.",
)
@with_provider_overrides
@click.pass_context
def connect_discord(
    ctx: click.Context,
    no_wait: bool,
    timeout: int,
    poll_interval: float,
) -> None:
    require_hotkey(ctx, group="config")
    client = build_client(ctx)
    try:
        authorization_url = client.create_discord_oauth_authorization_url()
        browser_opened = _open_authorization_url(authorization_url)
        discord_connected = _wait_for_discord_connection(
            client,
            no_wait=no_wait,
            timeout=timeout,
            poll_interval=poll_interval,
        )
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return

    next_action = (
        "discord_connected"
        if discord_connected is True
        else "open_authorization_url_and_complete_discord_oauth"
    )
    json_payload = {
        "authorization_url": authorization_url,
        "browser_opened": bool(browser_opened),
        "discord_connected": discord_connected,
        "extra_incentive_eligible": discord_connected
        if discord_connected is not None
        else None,
        "next_action": next_action,
    }
    if _json_mode(ctx):
        render(
            ctx,
            json_payload,
            warnings=discord_incentive_warnings(discord_connected),
        )
        return

    human_payload = {
        "discord_connected": discord_connected,
        "extra_incentive_eligible": discord_connected
        if discord_connected is not None
        else None,
        "authorization_url": authorization_url,
    }
    if not browser_opened:
        human_payload["browser_status"] = "Browser did not open automatically."

    summary = (
        "Discord connected; extra incentives enabled"
        if discord_connected is True
        else "Discord is not connected yet. Complete authorization using the URL below."
    )
    render(
        ctx,
        human_payload,
        summary=summary,
    )


@config_command.command(
    "set-subscriptions",
    short_help="Set machine-request GPU type subscriptions.",
)
@click.option(
    "--gpu",
    "gpu_types",
    multiple=True,
    help="GPU type to subscribe to (repeatable). Pass none to clear.",
)
@with_provider_overrides
@click.pass_context
def set_subscriptions(ctx: click.Context, gpu_types: tuple[str, ...]) -> None:
    require_hotkey(ctx, group="config")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.set_machine_request_subscription(list(gpu_types))
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(
        ctx,
        body,
        summary=f"subscriptions: [{', '.join(gpu_types) if gpu_types else 'cleared'}]",
    )


def _json_mode(ctx: click.Context) -> bool:
    opts = (ctx.obj or {}).get("provider_opts") or {}
    return bool(opts.get("json"))


def _open_authorization_url(authorization_url: str) -> bool:
    try:
        return bool(webbrowser.open(authorization_url))
    except Exception:
        return False


def _wait_for_discord_connection(
    client,
    *,
    no_wait: bool,
    timeout: int,
    poll_interval: float,
) -> bool | None:
    if no_wait:
        return _read_discord_connection(client)

    deadline = time.monotonic() + timeout
    while True:
        connected = _read_discord_connection(client)
        if connected is True:
            return True
        if time.monotonic() >= deadline:
            return connected
        time.sleep(poll_interval)


def _read_discord_connection(client) -> bool | None:
    return discord_connected_from_profile(client.whoami())


__all__ = ["config_command"]
