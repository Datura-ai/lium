"""``lium provider {earnings,idle-pay,ledger}`` -- what the account earned, idle pay per node, the ledger.

- ``earnings``         -- daily rental earnings of the account's hotkey (``--emissions``: the daily incentive).
- ``idle-pay [<id>]``  -- per idle node: paid / not_paid / rented_last_cycle / unknown, with the validator's reasons.
- ``ledger``           -- the signed-in account's ledger rows, one per UTC day and kind.

Read-only, so no persona gate; ``--json`` prints one envelope on stdout.
"""

from __future__ import annotations

from datetime import datetime

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import handle_provider_error, require_hotkey
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import fatal, render
from lium.provider.errors import OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT, PORTAL_FORBIDDEN, ProviderError

_DATE = click.DateTime(formats=["%Y-%m-%d"])

_IDLE_NODE_FIELDS = (
    "executor_id",
    "gpu_label",
    "gpu_count",
    "rented_gpu_count",
    "status",
    "tier",
    "idle_pay",
    "idle_pay_reasons",
    "idle_pay_checked_at",
)


def _day(value: datetime | None) -> str | None:
    return value.date().isoformat() if value else None


@click.command("earnings", short_help="Daily earnings of this account's hotkey.")
@click.option("--from", "date_from", type=_DATE, default=None, help="First UTC day (YYYY-MM-DD); default 7 days back.")
@click.option("--to", "date_to", type=_DATE, default=None, help="Last UTC day (YYYY-MM-DD); default today.")
@click.option("--node", "node_ids", multiple=True, help="Only this node (repeatable).")
@click.option("--emissions", is_flag=True, help="The daily incentive (emissions) instead of rental earnings.")
@click.option("--miner-hotkey", default=None, help="Another hotkey (ss58) instead of this account's.")
@with_provider_overrides
@click.pass_context
def earnings_command(
    ctx: click.Context,
    date_from: datetime | None,
    date_to: datetime | None,
    node_ids: tuple[str, ...],
    emissions: bool,
    miner_hotkey: str | None,
) -> None:
    """Rental earnings (or `--emissions`, the incentive) per UTC day, as the portal's Earnings page shows them."""
    require_hotkey(ctx, group="earnings")
    client = build_client(ctx)
    try:
        hotkey = miner_hotkey or client.own_hotkey()
        body = client.earnings_daily(
            hotkey,
            date_from=_day(date_from),
            date_to=_day(date_to),
            node_ids=list(node_ids) or None,
            emissions=emissions,
        )
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    kind = "emissions" if emissions else "earnings"
    render(ctx, body, summary=f"provider {kind}: hotkey={hotkey}")


@click.command("idle-pay", short_help="Idle pay per node, with the validator's reasons.")
@click.argument("node_id", required=False)
@with_provider_overrides
@click.pass_context
def idle_pay_command(ctx: click.Context, node_id: str | None) -> None:
    """For each node with a free GPU: `idle_pay` is `paid`, `not_paid` (with `idle_pay_reasons`, the
    validator's codes), `rented_last_cycle` or `unknown` (no validator cycle yet). A fully rented node has
    `idle_pay: null`. With NODE_ID only that node; one that is not yours is `node.not_found` (exit 5).

    The portal serves this overview to hotkey accounts only: an account created with e-mail or Google gets
    `portal.overview_not_for_custodied_account` (exit 6 under --json)."""
    require_hotkey(ctx, group="idle-pay")
    client = build_client(ctx)
    try:
        overview = client.provider_overview()
    except ProviderError as e:
        if e.code == PORTAL_FORBIDDEN:
            e = ProviderError(
                "the portal serves idle pay (its provider overview) to hotkey accounts only; this account was "
                "created with e-mail or Google",
                code=OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT,
                legacy_code=PORTAL_FORBIDDEN,
                hint="Sign in with the account's hotkey for idle pay (`lium provider -k <hotkey> idle-pay`); "
                "`lium provider node listing` works with this sign-in.",
                cause=e,
                context=e.context,
            )
        ctx.exit(handle_provider_error(ctx, e))
        return
    rows = overview.get("node_rows") if isinstance(overview.get("node_rows"), list) else []
    nodes = [{k: r.get(k) for k in _IDLE_NODE_FIELDS} for r in rows if isinstance(r, dict)]
    if node_id is not None:
        node = next((n for n in nodes if str(n.get("executor_id")) == node_id), None)
        if node is None:
            fatal(
                ctx,
                ProviderError(
                    f"node {node_id} is not one of this account's nodes",
                    code="node.not_found",
                    hint="`lium provider idle-pay` lists them.",
                ),
            )
            return
        render(ctx, node, summary=f"node {node_id}: idle pay {node.get('idle_pay') or 'n/a (fully rented)'}")
        return
    counts = overview.get("nodes") if isinstance(overview.get("nodes"), dict) else {}
    earned = overview.get("earned") if isinstance(overview.get("earned"), dict) else {}
    result = {
        "as_of": overview.get("as_of"),
        "idle_pay_usd": earned.get("idle_pay_usd"),
        "window_days": earned.get("window_days"),
        "idle": counts.get("idle"),
        "idle_earning": counts.get("idle_earning"),
        "idle_unpaid": counts.get("idle_unpaid"),
        "nodes": nodes,
    }
    render(
        ctx,
        result,
        summary=f"idle pay: earning={counts.get('idle_earning', 0)}, unpaid={counts.get('idle_unpaid', 0)}",
    )


@click.command("ledger", short_help="The account's ledger, one row per UTC day and kind.")
@click.option("--from", "date_from", type=_DATE, default=None, help="First UTC day (YYYY-MM-DD).")
@click.option("--to", "date_to", type=_DATE, default=None, help="Last UTC day (YYYY-MM-DD); default today.")
@with_provider_overrides
@click.pass_context
def ledger_command(ctx: click.Context, date_from: datetime | None, date_to: datetime | None) -> None:
    """The signed-in account's ledger (the account comes from the session, never an argument)."""
    require_hotkey(ctx, group="ledger")
    client = build_client(ctx)
    try:
        body = client.ledger_daily(date_from=_day(date_from), date_to=_day(date_to))
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary="provider ledger")


__all__ = ["earnings_command", "idle_pay_command", "ledger_command"]
