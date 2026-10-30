"""Helper to build a ``ProviderClient`` from a Click context (M2).

The flag → env → ``~/.lium/config.ini`` resolution happens once in the
``provider`` Click group callback, so by the time we reach here the values in
``ctx.obj["provider_opts"]`` are already merged.
"""

from __future__ import annotations

import click

from lium.cli.interactive import is_interactive
from lium.provider.client import ProviderClient
from lium.provider.errors import PORTAL_AUTH_TOTP_REQUIRED, ProviderAuthError


def build_client(ctx: click.Context, totp_code: str | None = None) -> ProviderClient:
    """Construct ``ProviderClient`` from the resolved opts in ``ctx.obj``; ``totp_code`` answers a sign-in that asks for one."""
    opts = (ctx.obj or {}).get("provider_opts") or {}
    client = ProviderClient(
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        portal_url=opts.get("portal_url"),
        account=opts.get("account"),
        api_token=opts.get("token"),
    )
    return attach_totp_code(client, totp_code)


def _ask() -> str:
    """The next code, or the portal's own refusal when nobody can be asked."""
    if not is_interactive():
        raise ProviderAuthError(
            "this step needs another authenticator code: run it in a terminal to be asked",
            code=PORTAL_AUTH_TOTP_REQUIRED,
        )
    return click.prompt("Authenticator code (or a recovery code)", err=True, hide_input=True)


def attach_totp_code(client: ProviderClient, totp_code: str | None) -> ProviderClient:
    """An account with an authenticator app signs in with a code; only an interactive terminal can be asked.

    A command that needs two codes (`config set-email`: the sign-in, then the address) asks the source
    twice, and the portal accepts each code once. The one `--totp-code` answers the first ask; a terminal
    is asked again, and a script without one gets the portal's refusal, not a silently reused code.
    """
    if totp_code:
        given = [totp_code]
        client.totp_code = lambda: given.pop() if given else _ask()
    elif is_interactive():
        client.totp_code = _ask
    return client


__all__ = ["attach_totp_code", "build_client"]
