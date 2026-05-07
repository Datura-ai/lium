"""Helper to build a ``MinerClient`` from a Click context (M2).

The flag → env → ``~/.lium/config.ini`` resolution happens once in the
``miner`` Click group callback, so by the time we reach here the values in
``ctx.obj["miner_opts"]`` are already merged.
"""

from __future__ import annotations

import click

from lium.miner.client import MinerClient


def build_client(ctx: click.Context) -> MinerClient:
    """Construct ``MinerClient`` from the resolved opts in ``ctx.obj``."""
    opts = (ctx.obj or {}).get("miner_opts") or {}
    return MinerClient(
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        portal_url=opts.get("portal_url"),
    )


__all__ = ["build_client"]
