"""``lium provider token {create,list,revoke}`` -- provider API tokens for agents.

A token (``lpk_…``) is created from a signed-in session (hotkey or ``portal login --email``), scoped
(``read``, ``node``, ``tier``, ``register``), listable and revocable. An agent then sets
``LIUM_PROVIDER_TOKEN=<token>`` and every ``lium provider`` command sends it as
``Authorization: Bearer`` instead of signing in with a wallet.

Until the portal serves the token routes these commands fail with ``portal.not_supported`` (exit 3).
"""

from __future__ import annotations

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import handle_provider_error, require_hotkey, require_persona_ack
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import render
from lium.provider.errors import ProviderError

SCOPES = ("read", "node", "tier", "register")


@click.group("token")
def token_command() -> None:
    """Create, list and revoke provider API tokens (LIUM_PROVIDER_TOKEN)."""


@token_command.command("create", short_help="Create a scoped provider API token.")
@click.option("--name", required=True, help="A label to recognise the token by in `token list`.")
@click.option(
    "--scope",
    "scopes",
    multiple=True,
    required=True,
    type=click.Choice(SCOPES),
    help="What the token may do (repeatable): read, node, tier, register.",
)
@click.option("--expires-days", type=click.IntRange(min=1), default=None, help="Days until it expires (portal default otherwise).")
@with_provider_overrides
@click.pass_context
def create(ctx: click.Context, name: str, scopes: tuple[str, ...], expires_days: int | None) -> None:
    """The secret is printed once, in this answer only; store it as LIUM_PROVIDER_TOKEN."""
    require_hotkey(ctx, group="token")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.create_api_token(name=name, scopes=list(dict.fromkeys(scopes)), expires_days=expires_days)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"token {name!r} created; the secret is shown only now (set LIUM_PROVIDER_TOKEN)")


@token_command.command("list", short_help="List this account's provider API tokens.")
@with_provider_overrides
@click.pass_context
def list_tokens(ctx: click.Context) -> None:
    """Name, scopes, created, last used and expiry of each token; never the secret."""
    require_hotkey(ctx, group="token")
    client = build_client(ctx)
    try:
        body = client.list_api_tokens()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary="provider API tokens")


@token_command.command("revoke", short_help="Revoke a provider API token.")
@click.argument("token_id", required=True)
@with_provider_overrides
@click.pass_context
def revoke(ctx: click.Context, token_id: str) -> None:
    """The token stops working at once."""
    require_hotkey(ctx, group="token")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.revoke_api_token(token_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body or {"id": token_id, "revoked": True}, summary=f"token {token_id} revoked")


__all__ = ["token_command"]
