"""``lium provider config …`` -- portal account & central-miner configuration.

Subcommands:

- ``show``                       -- ``GET /auth/me`` rich profile.
- ``opt-in / opt-out``           -- toggle the lium.io central miner server.
- ``set-email <email>``          -- update contact email.
- ``set-subscriptions``          -- machine-request notification subscriptions.

Distinction from ``lium config`` (CLI-side ConfigManager) and
``lium provider portal`` (JWT/session): this group is for *portal-account*
state held server-side. Mutating commands run the persona gate.
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
from lium.provider.errors import ProviderError


@click.group("config")
def config_command() -> None:
    """Provider account & central-miner-server configuration."""


@config_command.command("show", short_help="Full portal account profile.")
@click.pass_context
def show(ctx: click.Context) -> None:
    require_hotkey(ctx, group="config")
    client = build_client(ctx)
    try:
        body = client.whoami()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    summary_parts: list[str] = []
    if isinstance(body, dict):
        for key in ("miner_hotkey", "miner_uid", "email", "opt_in_status"):
            if key in body:
                summary_parts.append(f"{key}={body[key]}")
    summary = "config: " + ", ".join(summary_parts) if summary_parts else "config"
    render(ctx, body, summary=summary)


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
@click.pass_context
def opt_in(ctx: click.Context) -> None:
    _set_opt_in(ctx, True)


@config_command.command("opt-out", short_help="Run your own central miner server.")
@click.pass_context
def opt_out(ctx: click.Context) -> None:
    _set_opt_in(ctx, False)


@config_command.command("set-email", short_help="Update contact email.")
@click.argument("email", required=True)
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
    "set-subscriptions",
    short_help="Set machine-request GPU type subscriptions.",
)
@click.option(
    "--gpu",
    "gpu_types",
    multiple=True,
    help="GPU type to subscribe to (repeatable). Pass none to clear.",
)
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


__all__ = ["config_command"]
