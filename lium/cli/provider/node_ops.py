"""``lium provider node {register-token,tier,pause,resume,listing}`` -- the node steps an agent needs besides ``add``.

- ``register-token``            -- mint the one-hour token ``lium mine --register`` takes (the portal's Add Node line).
- ``tier eligibility <id>``     -- whether the node may change tier now, and what blocks it.
- ``tier set <id> secure|spot`` -- move the node to the Secure or Spot tier.
- ``pause <id>`` / ``resume <id> [--pause-id <uuid>]`` -- stop taking new rentals once the current one ends / take
  them again (with ``--pause-id``, only while that pause is still the node's current one).
- ``listing [<id>]``            -- each own node against the public listing: its state and the reasons it is hidden.

Every command takes ``--json`` (one envelope on stdout); the ones that change something ask the persona gate
(``--yes`` / ``LIUM_PROVIDER_ACK=1``; under ``--json`` or without a terminal ``input.confirmation_required``, exit 2).
Registered on the ``node`` group from ``command.py`` so ``node.py`` keeps only the node lifecycle.
"""

from __future__ import annotations

import shlex
import uuid

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import handle_provider_error, require_hotkey, require_persona_ack
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import fatal, render
from lium.provider.errors import ARG_INVALID, PAUSE_ID_MISMATCH, PORTAL_NOT_SUPPORTED, ProviderError

MINE_SH_URL = "https://raw.githubusercontent.com/Datura-ai/lium/main/mine.sh"
TIERS = ("secure", "spot")


@click.command("register-token", short_help="Mint a one-hour token for `lium mine --register`.")
@with_provider_overrides
@click.pass_context
def register_token(ctx: click.Context) -> None:
    """Mint a register token: it can only add a node to this account and watch its status, for one hour.

    Run the printed install line on the GPU host (or pass the token to `lium mine --register`).
    A register token cannot mint another one; sign in with a hotkey, `portal login --email` or an API token.
    """
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.create_register_token()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    token = str(body.get("token") or "")
    result = {
        "token": token,
        "issued_at": body.get("issued_at"),
        "expires_at": body.get("expires_at"),
        "install_command": f"curl -fsSL {MINE_SH_URL} | bash -s -- --register {shlex.quote(token)}",
    }
    render(ctx, result, summary=f"register token minted (expires {result['expires_at']})")


@click.group("tier")
def tier_group() -> None:
    """Show or change a node's tier (Secure or Spot)."""


@tier_group.command("eligibility", short_help="Whether the node may change tier now.")
@click.argument("node_id", required=True)
@with_provider_overrides
@click.pass_context
def tier_eligibility(ctx: click.Context, node_id: str) -> None:
    """`allowed` and the `blockers` ({code, message}: rented, cluster_member) the portal names."""
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        body = client.tier_change_eligibility(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"node {node_id}: tier change {'allowed' if body.get('allowed') else 'blocked'}")


@tier_group.command("set", short_help="Move the node to the Secure or Spot tier.")
@click.argument("node_id", required=True)
@click.argument("tier", type=click.Choice(TIERS, case_sensitive=False))
@with_provider_overrides
@click.pass_context
def tier_set(ctx: click.Context, node_id: str, tier: str) -> None:
    """A refused change is the portal's own code (`portal.tier_change_blocked`, the blocker in `error.data`)."""
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.update_tier(node_id, tier.lower())
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"node {node_id}: tier set to {tier.lower()}")


@click.command("pause", short_help="Take no new rental once the current one ends.")
@click.argument("node_id", required=True)
@with_provider_overrides
@click.pass_context
def pause(ctx: click.Context, node_id: str) -> None:
    """Pause new rentals on a rented node; the current rental runs on. The portal refuses an idle node
    (`portal.node_not_rented`).

    `paused_by_this_call` is true only when this call set the pause, and `pause_id` is then this call's own:
    keep it for `node resume --pause-id`. On a node already paused it is false and `pause_id` is the existing
    pause's, which may be null. A portal that predates `pause_id` sends neither: both are null, never false.

    Already paused: `node get --json` has `new_rentals_pause_requested_at` set, and in `node listing --json`
    `computed_status.status` is PAUSING_NEW_RENTALS (the rental still runs) or NEW_RENTALS_PAUSED, with
    NEW_RENTALS_PAUSED in `hidden_reasons[].code`."""
    require_hotkey(ctx, group="node")
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        body = client.pause_new_rentals(node_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    already = body.get("paused_by_this_call") is False
    render(ctx, body, summary=f"node {node_id}: new rentals {'already paused, not by this call' if already else 'paused'}")


@click.command("resume", short_help="Take new rentals again.")
@click.argument("node_id", required=True)
@click.option(
    "--pause-id",
    "pause_id",
    metavar="UUID",
    default=None,
    help="Resume only if this is still the node's current pause (the `pause_id` `node pause` returned). "
    "Otherwise nothing changes and the command fails with node.pause_id_mismatch (exit 3), "
    "the current pause in error.data.current_pause_id.",
)
@with_provider_overrides
@click.pass_context
def resume(ctx: click.Context, node_id: str, pause_id: str | None) -> None:
    """Undo `node pause`. Without --pause-id this lifts whatever pause is set.

    With --pause-id the command first reads the node: a portal whose `node get` has no `pause_id` field would
    ignore the id and resume anyway, so the command refuses there (portal.not_supported, exit 3) and sends
    no resume. A malformed id is input.arg_invalid (exit 2 under --json, LIUM_OUTPUT=json or LIUM_NONINTERACTIVE=1)."""
    require_hotkey(ctx, group="node")
    if pause_id is not None:
        try:
            pause_id = str(uuid.UUID(pause_id.strip()))
        except ValueError:
            fatal(ctx, _malformed_pause_id(pause_id))
            return
    require_persona_ack(ctx)
    client = build_client(ctx)
    try:
        if pause_id is not None:
            _require_pause_id_support(client, node_id, pause_id)
        body = client.resume_new_rentals(node_id, pause_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, _resume_error(e, node_id, pause_id)))
        return
    render(ctx, body, summary=f"node {node_id}: new rentals resumed")


def _malformed_pause_id(value: str) -> ProviderError:
    return ProviderError(
        f"--pause-id {value!r} is not a UUID",
        code=ARG_INVALID,
        hint="Pass the `pause_id` that `lium provider node pause --json` returned.",
        context={"option": "--pause-id", "value": value},
    )


def _require_pause_id_support(client, node_id: str, pause_id: str) -> None:
    """Raise unless the node's record has a `pause_id` key: a portal without one ignores `?pause_id=`."""
    node = client.get_node(node_id)
    if isinstance(node, dict) and "pause_id" in node:
        return
    raise ProviderError(
        f"not resumed: this portal does not report pause ids, so it would resume node {node_id} whatever "
        "--pause-id says",
        code=PORTAL_NOT_SUPPORTED,
        hint="No resume was sent. Leave the node paused, or resume it without --pause-id only if you know "
        "the pause is yours.",
        context={"node_id": node_id, "pause_id": pause_id, "unsupported": "pause_id"},
    )


def _resume_error(err: ProviderError, node_id: str, pause_id: str | None) -> ProviderError:
    """The portal's 409 PAUSE_ID_MISMATCH as `node.pause_id_mismatch`, and its 422 for the id as `ARG_INVALID`."""
    if pause_id is None:
        return err
    status = err.context.get("status")
    if status == 409 and err.context.get("portal_code") == "PAUSE_ID_MISMATCH":
        detail = err.context.get("detail") or {}
        return ProviderError(
            err.message,
            code=PAUSE_ID_MISMATCH,
            context={
                "node_id": node_id,
                "pause_id": pause_id,
                "current_pause_id": detail.get("current_pause_id"),
                "status": status,
                "portal_code": "PAUSE_ID_MISMATCH",
            },
        )
    if status == 422 and err.context.get("method") == "DELETE":
        return _malformed_pause_id(pause_id)
    return err


@click.command("listing", short_help="Each own node against the public listing.")
@click.argument("node_id", required=False)
@with_provider_overrides
@click.pass_context
def listing(ctx: click.Context, node_id: str | None) -> None:
    """`listing_state` (rented, listed, hidden, offline, validating) and the `hidden_reasons` that keep a
    node off the listing. With NODE_ID only that node; a node that is not yours is `node.not_found` (exit 5).

    `rented_gpu_count` of `gpu_count` GPUs are rented now: a node rented in part stays `listed` for its free
    GPUs, so check `rented_gpu_count` before anything that interrupts a rental.

    Each row also carries `new_rentals_pause_requested_at` and `pause_id` (null when new rentals are not
    paused, or the portal does not send them)."""
    require_hotkey(ctx, group="node")
    client = build_client(ctx)
    try:
        rows = client.nodes_listing()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    if node_id is None:
        listed = sum(1 for r in rows if r.get("listing_state") == "listed")
        with_renter = sum(1 for r in rows if (r.get("rented_gpu_count") or 0) > 0)
        render(ctx, rows, summary=f"node listing: nodes={len(rows)}, listed={listed}, with a renter={with_renter}")
        return
    row = next((r for r in rows if str(r.get("id")) == node_id), None)
    if row is None:
        fatal(
            ctx,
            ProviderError(
                f"node {node_id} is not one of this account's nodes",
                code="node.not_found",
                hint="`lium provider node listing` lists them.",
            ),
        )
        return
    summary = f"node {node_id}: {row.get('listing_state')}"
    if row.get("rented_gpu_count") is not None and row.get("gpu_count"):
        summary += f", {row['rented_gpu_count']}/{row['gpu_count']} GPUs rented"
    render(ctx, row, summary=summary)


def register_node_ops(group: click.Group) -> None:
    """Add these commands to ``lium provider node``."""
    group.add_command(register_token)
    group.add_command(tier_group)
    group.add_command(pause)
    group.add_command(resume)
    group.add_command(listing)


__all__ = ["register_node_ops"]
