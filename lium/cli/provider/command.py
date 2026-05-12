"""``lium provider`` Click group + global options + persona gate (A4).

Subcommands:

- ``lium provider portal {login,logout,whoami}`` -- portal session management.
- ``lium provider status`` -- aggregated provider snapshot.
- ``lium provider node {list,get,add,rm,update-price,update-gpu,
  min-gpu,pods,machine-requests,notice-period,notify-added}``
  -- node lifecycle on the portal.
- ``lium provider config {show,opt-in,opt-out,set-email,set-subscriptions}``
  -- portal-account configuration. ``opt-in/opt-out`` toggles the lium.io
  central miner server.
- ``lium provider sync {from-miner-server,to-miner-server}`` -- batch sync
  between the portal and the central miner.
- ``lium provider {billing,machine-request,machine}``
  -- read-only history queries.

Hotkey registration on SN51 is handled directly by ``btcli subnet register``;
the CLI persists ``--coldkey``/``--hotkey`` via ``lium config set
provider.coldkey ...`` / ``provider.hotkey ...`` so subsequent commands inherit
them without re-prompting.
"""

from __future__ import annotations

import click

from lium.cli.provider._persona import confirm_persona
from lium.cli.provider._render import emit_error, fatal
from lium.cli.provider.config import config_command
from lium.cli.provider.node import node_command
from lium.cli.provider.portal import portal_command
from lium.cli.provider.queries import (
    billing_command,
    machine_command,
    machine_request_command,
)
from lium.cli.provider.status import status_command
from lium.cli.provider.sync import sync_command
from lium.cli.settings import ConfigManager
from lium.provider.errors import ProviderError


@click.group("provider")
@click.option(
    "--coldkey",
    "-w",
    "coldkey",
    envvar="LIUM_PROVIDER_COLDKEY",
    help="Bittensor coldkey (wallet) name. Falls back to LIUM_PROVIDER_COLDKEY "
    "and then `provider.coldkey` in ~/.lium/config.ini.",
)
@click.option(
    "--hotkey",
    "-k",
    "hotkey",
    envvar="LIUM_PROVIDER_HOTKEY",
    help="Bittensor hotkey name on the coldkey. Falls back to LIUM_PROVIDER_HOTKEY "
    "and then `provider.hotkey` in ~/.lium/config.ini.",
)
@click.option(
    "--portal-url",
    "portal_url",
    envvar="LIUM_PORTAL_URL",
    help="Override the lium-miner-portal base URL (default: production).",
)
@click.option(
    "--json",
    "json_mode",
    is_flag=True,
    help="Emit machine-readable JSON output (one envelope per command).",
)
@click.option(
    "--debug",
    "debug",
    is_flag=True,
    help="Include error context in stderr; verbose logging.",
)
@click.option(
    "--yes",
    "-y",
    "yes_flag",
    is_flag=True,
    help="Auto-confirm the persona gate for spend-affecting subcommands.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="Skip irreversible subprocess calls (e.g. ssh) and report intent only.",
)
@click.pass_context
def provider_command(
    ctx: click.Context,
    coldkey: str | None,
    hotkey: str | None,
    portal_url: str | None,
    json_mode: bool,
    debug: bool,
    yes_flag: bool,
    dry_run: bool,
) -> None:
    """Provider-side commands for Subnet 51 mining.

    Use ``lium mine`` for renter workflows. ``lium provider ...`` is the
    provider persona: portal session management, managing nodes,
    installing GPU nodes, and reporting validator weights. Hotkey
    registration on SN51 itself is done directly with ``btcli subnet
    register``.
    """
    # Resolve coldkey/hotkey/portal_url once, here, so every subcommand sees
    # the same merged view: ``--flag`` → ``LIUM_PROVIDER_*`` env (handled by
    # Click) → ``[provider]`` section of ``~/.lium/config.ini``. Doing it here
    # (rather than only in build_client) means early arg checks in
    # subcommands -- e.g. "portal login requires --hotkey" -- also honour
    # the config-file fallback.
    cfg = ConfigManager()
    ctx.ensure_object(dict)
    ctx.obj["provider_opts"] = {
        "coldkey": coldkey or cfg.get("provider.coldkey"),
        "hotkey": hotkey or cfg.get("provider.hotkey"),
        "portal_url": portal_url or cfg.get("provider.portal_url"),
        "json": json_mode,
        "debug": debug,
        "yes": yes_flag,
        "dry_run": dry_run,
    }

    # Persona gate is invoked from each spend-affecting subcommand instead of
    # here -- otherwise ``lium provider <subcmd> --help`` would prompt before
    # Click could short-circuit on the help flag.
    del coldkey, hotkey, yes_flag


@provider_command.result_callback()
@click.pass_context
def _provider_result(ctx: click.Context, result, **_kwargs) -> None:
    """No-op result hook reserved for future post-command bookkeeping."""
    del ctx, result


provider_command.add_command(portal_command, name="portal")
provider_command.add_command(status_command, name="status")
provider_command.add_command(node_command, name="node")
provider_command.add_command(config_command, name="config")
provider_command.add_command(sync_command, name="sync")
provider_command.add_command(billing_command, name="billing")
provider_command.add_command(machine_request_command, name="machine-request")
provider_command.add_command(machine_command, name="machine")


# Re-exported for symmetry with other CLI subgroups.
def handle_provider_error(ctx: click.Context, err: ProviderError) -> int:
    """Map a ProviderError to its exit code; used by subcommand try/except."""
    return emit_error(ctx, err)


def enforce_persona_gate(ctx: click.Context) -> None:
    """Run the persona gate for spend-affecting subcommands.

    Called as the first action of any subcommand that takes a spend-affecting
    or otherwise-irreversible action (register, node mutations,
    install). If the user declines, exits with ``ARG_INVALID`` exit code.
    """
    opts = (ctx.obj or {}).get("provider_opts") or {}
    ok = confirm_persona(
        ctx,
        coldkey=opts.get("coldkey"),
        hotkey=opts.get("hotkey"),
        yes_flag=bool(opts.get("yes")),
    )
    if not ok:
        fatal(
            ctx,
            ProviderError(
                "persona confirmation declined; aborting spend-affecting command",
                code="ARG_INVALID",
                hint="Re-run with --yes or set LIUM_PROVIDER_ACK=1.",
            ),
        )


__all__ = ["enforce_persona_gate", "handle_provider_error", "provider_command"]
