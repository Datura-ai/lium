"""Shared guards for ``lium provider …`` subcommands.

Two gates that every spend-affecting subcommand needs:

- ``require_hotkey`` -- ensures the portal can be signed in to: a hotkey
  resolvable from ``--hotkey`` / ``LIUM_PROVIDER_HOTKEY`` / ``provider.hotkey``
  config, or a bearer token (``LIUM_PROVIDER_TOKEN``, or the session of
  ``portal login --email``). Read-only commands also use this so they fail
  fast with a clear hint.
- ``require_persona_ack`` -- a fresh shell prompts once for confirmation
  before any irreversible portal mutation. ``--yes`` / ``LIUM_PROVIDER_ACK=1`` /
  per-shell ack short-circuit silently; in agent mode (``--json``,
  ``LIUM_OUTPUT=json``, ``LIUM_NONINTERACTIVE=1``) it fails with
  ``input.confirmation_required`` (exit 2) instead of asking.

Centralising these here removes the same 5-line guard repeated across
``node.py``, ``config.py``, ``sync.py``, ``queries.py``.

Path-segment validation lives in :mod:`lium.provider.client` (private
helpers ``_safe_id`` / ``_safe_hotkey_segment``); see that file for the
SDK-side defense-in-depth check.
"""

from __future__ import annotations

import click

from lium.cli.provider._client import PROVIDER_TOKEN_ENV, bearer_token, session_email
from lium.cli.provider._persona import ConfirmationRequired, confirm_persona
from lium.cli.provider._render import agent_mode, emit_error, fatal
from lium.provider.errors import ARG_INVALID, CONFIRMATION_REQUIRED, NOT_SIGNED_IN, ProviderError


def not_signed_in_error() -> ProviderError:
    """``auth.not_signed_in``: no provider token, no hotkey and no live e-mail session. Plain text mode prints it
    as ``ARG_INVALID`` (exit 1); the hint names the three ways in, and the configured e-mail address if any."""
    email = session_email()
    return ProviderError(
        f"not signed in to the provider portal (no live e-mail session for {email})"
        if email
        else "not signed in to the provider portal",
        code=NOT_SIGNED_IN,
        legacy_code=ARG_INVALID,
        hint=f"Set {PROVIDER_TOKEN_ENV} to a provider API token, or run "
        f"`lium provider portal login --email {email or '<address>'}` with the password in LIUM_PROVIDER_PASSWORD, "
        "or pass --hotkey (or LIUM_PROVIDER_HOTKEY) for a wallet on this machine.",
        context={"session_email": email} if email else None,
    )


def require_hotkey(ctx: click.Context, *, group: str | None = None) -> None:
    """Exit if neither a hotkey nor a bearer token is configured.

    In agent mode (``--json``, ``LIUM_OUTPUT=json``, ``LIUM_NONINTERACTIVE=1``) that is ``auth.not_signed_in``
    (exit 6). Plain text mode keeps the old ``ARG_INVALID`` line, with ``group`` (optional) folded into the
    message so the user knows which subgroup needs it.
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if opts.get("hotkey") or bearer_token(opts):
        return
    if agent_mode(ctx):
        fatal(ctx, not_signed_in_error())
        return
    label = f"{group} commands" if group else "this command"
    fatal(
        ctx,
        ProviderError(
            f"{label} require --hotkey (or LIUM_PROVIDER_HOTKEY)",
            code=ARG_INVALID,
        ),
    )


def require_wallet_hotkey(ctx: click.Context, *, command: str) -> None:
    """Exit unless a hotkey is set: ``command`` signs with the hotkey's wallet, which a provider API token or an
    e-mail session cannot stand in for.

    Agent mode answers ``input.arg_invalid`` (exit 2) saying so; plain text mode prints the old
    ``config commands require --hotkey`` line (exit 1).
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if opts.get("hotkey"):
        return
    if not agent_mode(ctx):
        fatal(ctx, ProviderError("config commands require --hotkey (or LIUM_PROVIDER_HOTKEY)", code=ARG_INVALID))
        return
    fatal(
        ctx,
        ProviderError(
            f"{command} requires --hotkey (or LIUM_PROVIDER_HOTKEY): it signs with the hotkey's wallet, "
            "and a provider API token or e-mail session cannot",
            code=ARG_INVALID,
            hint="Re-run with --hotkey (or LIUM_PROVIDER_HOTKEY) for a wallet on this machine, "
            "or change it in the provider portal.",
            context={"requires": "hotkey"},
        ),
    )


def require_persona_ack(ctx: click.Context) -> None:
    """Run the persona gate before any spend-affecting subcommand."""
    opts = (ctx.obj or {}).get("provider_opts") or {}
    try:
        ok = confirm_persona(
            ctx,
            coldkey=opts.get("coldkey"),
            hotkey=opts.get("hotkey"),
            yes_flag=bool(opts.get("yes")),
            json_mode=bool(opts.get("json")),
        )
    except ConfirmationRequired as e:
        fatal(
            ctx,
            ProviderError(
                f"confirmation required before a spend-affecting command ({e})",
                code=CONFIRMATION_REQUIRED,
                hint="Re-run with --yes, or set LIUM_PROVIDER_ACK=1 once for this agent's environment.",
                context={"flag": "--yes", "env": "LIUM_PROVIDER_ACK=1"},
            ),
        )
        return
    if ok:
        return
    fatal(
        ctx,
        ProviderError(
            "persona confirmation declined; aborting spend-affecting command",
            code=ARG_INVALID,
            hint="Re-run with --yes or set LIUM_PROVIDER_ACK=1.",
        ),
    )


def handle_provider_error(ctx: click.Context, err: Exception) -> int:
    """Render a ``ProviderError`` and return its exit code."""
    if isinstance(err, ProviderError):
        return emit_error(ctx, err)
    raise err


__all__ = [
    "handle_provider_error",
    "not_signed_in_error",
    "require_hotkey",
    "require_persona_ack",
    "require_wallet_hotkey",
]
