"""Helper to build a ``ProviderClient`` from a Click context (M2).

The flag → env → ``~/.lium/config.ini`` resolution happens once in the
``provider`` Click group callback, so by the time we reach here the values in
``ctx.obj["provider_opts"]`` are already merged.

Which credential a command signs in with, first match wins:

1. ``LIUM_PROVIDER_TOKEN`` -- a provider API token, sent as ``Authorization: Bearer``;
2. the hotkey (``--hotkey`` / ``LIUM_PROVIDER_HOTKEY`` / ``provider.hotkey``) and its wallet;
3. the session ``portal login --email`` stored for ``provider.email`` (or ``LIUM_PROVIDER_EMAIL``).
"""

from __future__ import annotations

import os
from typing import Any, Mapping

import click

from lium.cli.settings import ConfigManager
from lium.provider.client import ProviderClient, email_session_key
from lium.provider.token_store import TokenStore

PROVIDER_TOKEN_ENV = "LIUM_PROVIDER_TOKEN"
PROVIDER_EMAIL_ENV = "LIUM_PROVIDER_EMAIL"


def session_email() -> str | None:
    """The address of the e-mail sign-in to use: ``LIUM_PROVIDER_EMAIL``, else ``provider.email``."""
    return (os.environ.get(PROVIDER_EMAIL_ENV) or "").strip() or ConfigManager().get("provider.email") or None


def bearer_token(opts: Mapping[str, Any]) -> str | None:
    """The bearer token this invocation sends instead of a hotkey session, if any."""
    token = (os.environ.get(PROVIDER_TOKEN_ENV) or "").strip()
    if token:
        return token
    if opts.get("hotkey"):
        return None
    email = session_email()
    if not email:
        return None
    cached = TokenStore().load(email_session_key(email))   # a corrupt or expired entry is None; a lock timeout is raised
    return cached.token if cached is not None else None


AUTH_TOKEN = "token"
AUTH_HOTKEY = "hotkey"
AUTH_EMAIL_SESSION = "email_session"


def auth_method(opts: Mapping[str, Any]) -> str | None:
    """Which credential this invocation signs in with, by the order above: ``token``, ``hotkey``,
    ``email_session``, or None when there is none (an e-mail session that has ended counts as none)."""
    if (os.environ.get(PROVIDER_TOKEN_ENV) or "").strip():
        return AUTH_TOKEN
    if opts.get("hotkey"):
        return AUTH_HOTKEY
    if bearer_token(opts):
        return AUTH_EMAIL_SESSION
    return None


def build_client(ctx: click.Context, *, wallet_only: bool = False) -> ProviderClient:
    """Construct ``ProviderClient`` from the resolved opts in ``ctx.obj``.

    ``wallet_only`` drops the bearer token, for the commands that sign with the hotkey's wallet whatever
    else is set (``config set-email``, ``config set-password``).
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    return ProviderClient(
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        portal_url=opts.get("portal_url"),
        api_token=None if wallet_only else bearer_token(opts),
    )


__all__ = [
    "AUTH_EMAIL_SESSION",
    "AUTH_HOTKEY",
    "AUTH_TOKEN",
    "PROVIDER_EMAIL_ENV",
    "PROVIDER_TOKEN_ENV",
    "auth_method",
    "bearer_token",
    "build_client",
    "session_email",
]
