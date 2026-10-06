"""Helper to build a ``ProviderClient`` from a Click context (M2).

The flag → env → ``~/.lium/config.ini`` resolution happens once in the
``provider`` Click group callback, so by the time we reach here the values in
``ctx.obj["provider_opts"]`` are already merged.
"""

from __future__ import annotations

import click

from lium.provider.client import ProviderClient


def build_client(ctx: click.Context) -> ProviderClient:
    """Construct ``ProviderClient`` from the resolved opts in ``ctx.obj``."""
    opts = (ctx.obj or {}).get("provider_opts") or {}
    return ProviderClient(
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        portal_url=opts.get("portal_url"),
    )


__all__ = ["build_client"]
