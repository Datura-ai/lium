"""Shared guards for ``lium provider …`` subcommands.

Two gates that every spend-affecting subcommand needs:

- ``require_hotkey`` -- ensures a hotkey is resolvable from
  ``--hotkey`` / ``LIUM_PROVIDER_HOTKEY`` / ``provider.hotkey`` config.
  Read-only commands also use this so they fail fast with a clear hint.
- ``require_persona_ack`` -- forwards to ``enforce_persona_gate`` from
  ``command.py`` so a fresh shell prompts once for confirmation before any
  irreversible portal mutation. ``--yes`` / ``LIUM_PROVIDER_ACK=1`` /
  per-shell ack short-circuit silently.

Centralising these here removes the same 5-line guard repeated across
``node.py``, ``config.py``, ``sync.py``, ``queries.py``.

Path-segment validation lives in :mod:`lium.provider.client` (private
helpers ``_safe_id`` / ``_safe_hotkey_segment``); see that file for the
SDK-side defense-in-depth check.
"""

from __future__ import annotations

import click

from lium.cli.provider._persona import confirm_persona
from lium.cli.provider._render import emit_error, fatal
from lium.provider.errors import ARG_INVALID, ProviderError


def require_hotkey(ctx: click.Context, *, group: str | None = None) -> None:
    """Exit with a clear ARG_INVALID if no hotkey is configured.

    ``group`` (optional) is folded into the message so the user knows
    which subgroup needs it.
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    if opts.get("hotkey"):
        return
    label = f"{group} commands" if group else "this command"
    fatal(
        ctx,
        ProviderError(
            f"{label} require --hotkey (or LIUM_PROVIDER_HOTKEY)",
            code=ARG_INVALID,
        ),
    )


def require_persona_ack(ctx: click.Context) -> None:
    """Run the persona gate before any spend-affecting subcommand.

    Mirrors :func:`lium.cli.provider.command.enforce_persona_gate` but lives
    here to avoid a circular import (the subgroup modules in turn import
    ``command.py`` only via the umbrella).
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    ok = confirm_persona(
        ctx,
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        yes_flag=bool(opts.get("yes")),
    )
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
    "require_hotkey",
    "require_persona_ack",
]
