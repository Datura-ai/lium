"""Output rendering + exit-code mapping for ``lium miner …``.

Two output modes:

- ``--json``: deterministic ``{ok, data, error, warnings}`` envelope, one
  line per result. Driven by an agent.
- TTY default: short human-readable text. Rich tables are deliberately kept
  simple in M2 (single-line summaries) so we can extend later without
  breaking parsers.

Exit codes follow the taxonomy in :mod:`lium.miner.errors` (see module
docstring). Returning the int from a Click command via
``ctx.exit(emit_error(ctx, err))`` keeps every command's error path
identical.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Mapping

import click

from lium.miner.errors import (
    ARG_INVALID,
    CONFIG_MISSING,
    HOTKEY_NOT_REGISTERED,
    PORTAL_AUTH_EXPIRED,
    PORTAL_AUTH_INVALID,
    PORTAL_AUTH_REFRESH_RACE,
    PORTAL_CONTRACT_DRIFT,
    PORTAL_NOT_FOUND,
    PORTAL_RATE_LIMIT,
    PORTAL_SERVER_ERROR,
    PORTS_INVALID,
    SSH_AUTH_FAILED,
    SSH_UNREACHABLE,
    WALLET_NOT_FOUND,
    MinerError,
)


# code -> exit-code mapping. Anything missing falls back to ``1``.
_EXIT_CODES: dict[str, int] = {
    # 1: user/arg errors
    ARG_INVALID: 1,
    PORTS_INVALID: 1,
    HOTKEY_NOT_REGISTERED: 1,
    # 2: auth errors
    PORTAL_AUTH_INVALID: 2,
    PORTAL_AUTH_EXPIRED: 2,
    WALLET_NOT_FOUND: 2,
    # 3: portal (server-side)
    PORTAL_SERVER_ERROR: 3,
    PORTAL_NOT_FOUND: 3,
    PORTAL_CONTRACT_DRIFT: 3,
    PORTAL_RATE_LIMIT: 3,
    # 5: ssh
    SSH_UNREACHABLE: 5,
    SSH_AUTH_FAILED: 5,
    # 6: config
    CONFIG_MISSING: 6,
    # 7: token-cache contention
    PORTAL_AUTH_REFRESH_RACE: 7,
}


def exit_code_for(err: MinerError) -> int:
    """Map a :class:`MinerError` to its CLI exit status."""
    return _EXIT_CODES.get(err.code, 1)


def render(
    ctx: click.Context,
    payload: Any,
    *,
    summary: str | None = None,
) -> None:
    """Render a successful result.

    JSON mode emits a single line ``{"ok": true, "data": ...}``; TTY mode
    prints ``summary`` (if provided) followed by a compact key/value dump.
    """
    json_mode = _json_mode(ctx)
    if json_mode:
        envelope = {"ok": True, "data": _to_serialisable(payload)}
        click.echo(json.dumps(envelope, sort_keys=True, default=str))
        return

    if summary:
        click.echo(summary)
    body = _to_serialisable(payload)
    if isinstance(body, dict):
        for key, value in body.items():
            if isinstance(value, (dict, list)):
                click.echo(f"  {key}: {json.dumps(value, default=str)}")
            else:
                click.echo(f"  {key}: {value}")


def emit_error(ctx: click.Context, err: MinerError) -> int:
    """Format a :class:`MinerError` and return its exit code."""
    code = exit_code_for(err)
    if _json_mode(ctx):
        envelope = {"ok": False, "error": err.to_dict()}
        click.echo(json.dumps(envelope, sort_keys=True, default=str), err=True)
    else:
        prefix = click.style(f"[{err.code}]", fg="red", bold=True)
        click.echo(f"{prefix} {err.message}", err=True)
        if err.hint:
            click.echo(f"  hint: {err.hint}", err=True)
        if _debug_mode(ctx) and err.context:
            click.echo(f"  context: {err.context}", err=True)
    return code


def emit_warning(ctx: click.Context, code: str, message: str) -> None:
    """Surface a non-fatal warning (e.g. PORTAL_LOGIN_REPLAY_DEBT)."""
    if _json_mode(ctx):
        # Warnings ride alongside the next render() call; for now emit
        # nothing to keep the JSON stream clean. The status command surfaces
        # warnings via MinerStatus.warnings instead.
        return
    click.echo(
        click.style(f"[{code}]", fg="yellow", bold=True) + f" {message}",
        err=True,
    )


def _json_mode(ctx: click.Context) -> bool:
    return bool(_opts(ctx).get("json"))


def _debug_mode(ctx: click.Context) -> bool:
    return bool(_opts(ctx).get("debug"))


def _opts(ctx: click.Context) -> Mapping[str, Any]:
    obj = ctx.obj or {}
    return obj.get("miner_opts") or {}


def _to_serialisable(payload: Any) -> Any:
    """Convert pydantic models / dataclasses / nested structures to JSON-safe."""
    if hasattr(payload, "model_dump"):
        return payload.model_dump()
    if isinstance(payload, dict):
        return {k: _to_serialisable(v) for k, v in payload.items()}
    if isinstance(payload, (list, tuple)):
        return [_to_serialisable(v) for v in payload]
    return payload


def fatal(ctx: click.Context, err: MinerError) -> None:
    """Convenience: emit the error and exit with the mapped status code."""
    code = emit_error(ctx, err)
    sys.exit(code)


__all__ = [
    "emit_error",
    "emit_warning",
    "exit_code_for",
    "fatal",
    "render",
]
