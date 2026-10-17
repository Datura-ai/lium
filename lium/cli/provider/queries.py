"""``lium provider {billing,machine-request,machine}`` queries.

Read-only listings that mirror the portal's query endpoints. Each command
accepts pagination + optional filters and renders the raw envelope so
unknown future fields surface without a model bump.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

import click

from lium.cli.provider._client import build_client
from lium.cli.provider._guards import handle_provider_error, require_hotkey
from lium.cli.provider._overrides import with_provider_overrides
from lium.cli.provider._render import _json_mode, fatal, render
from lium.provider.errors import ARG_INVALID, ProviderError

# The portal's /provider-ledger/daily window: 30 days by default, at most 90 (routes/provider_ledger.py).
_LEDGER_DEFAULT_DAYS = 30
_LEDGER_MAX_DAYS = 90
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Billing


@click.group("billing")
def billing_command() -> None:
    """Billing history queries."""


@billing_command.command("list", short_help="Your daily earnings ledger.")
@click.option("--from", "start_day", metavar="YYYY-MM-DD", default=None, help="First UTC day (default: 30 days ago).")
@click.option("--to", "end_day", metavar="YYYY-MM-DD", default=None, help="Last UTC day (default: today; at most 90 days).")
@with_provider_overrides
@click.pass_context
def list_billing(ctx: click.Context, start_day: str | None, end_day: str | None) -> None:
    """The signed-in provider's own earnings, one row per UTC day and kind."""
    require_hotkey(ctx, group="billing")
    _check_ledger_range(ctx, start_day, end_day)
    client = build_client(ctx)
    try:
        body = client.billing_history(start_day=start_day, end_day=end_day)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    # the transport unwraps the DetailResponse, so `rows` is top level
    rows = body.get("rows") if isinstance(body, dict) else None
    rows = rows if isinstance(rows, list) else []
    if _json_mode(ctx):
        render(ctx, body)
        return
    period = {key: body.get(key) for key in ("date_from", "date_to")}
    render(ctx, {"data": rows, **period}, summary=f"ledger rows: {len(rows)}")


def _check_ledger_range(ctx: click.Context, start_day: str | None, end_day: str | None) -> None:
    """Refuse a bad day or window before the request: the portal's 422 would read as schema drift."""

    def day(option: str, value: str | None) -> date | None:
        if value is None:
            return None
        try:
            if _DAY_RE.match(value):
                return date.fromisoformat(value)
        except ValueError:
            pass
        fatal(ctx, ProviderError(f"{option} must be a UTC day as YYYY-MM-DD, not '{value}'", code=ARG_INVALID))

    end = day("--to", end_day) or datetime.now(timezone.utc).date()
    start = day("--from", start_day) or end - timedelta(days=_LEDGER_DEFAULT_DAYS - 1)
    if start > end:
        fatal(ctx, ProviderError("--from must be on or before --to", code=ARG_INVALID))
    if (end - start).days + 1 > _LEDGER_MAX_DAYS:
        fatal(
            ctx,
            ProviderError(
                f"the window is {(end - start).days + 1} days; at most {_LEDGER_MAX_DAYS}",
                code=ARG_INVALID,
                hint="Split it into several calls of 90 days or fewer.",
            ),
        )


# ---------------------------------------------------------------------------
# Machine requests


@click.group("machine-request")
def machine_request_command() -> None:
    """Pending tenant machine requests."""


@machine_request_command.command("list", short_help="All pending tenant requests.")
@with_provider_overrides
@click.pass_context
def list_machine_requests(ctx: click.Context) -> None:
    """Pending tenant requests, or their aggregate when the hotkey has no validator-verified node."""
    require_hotkey(ctx, group="machine-request")
    client = build_client(ctx)
    try:
        body = client.list_machine_requests()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=_machine_request_summary_line(body))


def _machine_request_summary_line(body: object) -> str:
    """The one-line summary above the table: the aggregate tier states why it carries no rows."""
    if isinstance(body, dict) and body.get("tier") == "aggregate":
        return (
            f"machine requests: {body.get('open_requests') or 0} open "
            "(aggregate — per-request detail needs a portal login and a validator-verified node)"
        )
    rows = body.get("data") if isinstance(body, dict) else body
    count = len(rows) if isinstance(rows, list) else 0
    return f"machine requests: {count}"


@machine_request_command.command("get", short_help="Single tenant machine request.")
@click.argument("request_id", required=True)
@with_provider_overrides
@click.pass_context
def get_machine_request(ctx: click.Context, request_id: str) -> None:
    require_hotkey(ctx, group="machine-request")
    client = build_client(ctx)
    try:
        body = client.get_machine_request(request_id)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"machine request {request_id}")


# ---------------------------------------------------------------------------
# Machines (catalogue)


@click.group("machine")
def machine_command() -> None:
    """GPU machine catalog + reward estimates."""


@machine_command.command("list", short_help="Available GPU machine catalog.")
@with_provider_overrides
@click.pass_context
def list_machines(ctx: click.Context) -> None:
    require_hotkey(ctx, group="machine")
    client = build_client(ctx)
    try:
        body = client.list_machines()
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    rows = body.get("data") if isinstance(body, dict) else body
    count = len(rows) if isinstance(rows, list) else 0
    render(ctx, body, summary=f"machines: {count}")


@machine_command.command("estimate", short_help="Estimated rewards for a GPU type.")
@click.option("--gpu-type", required=True, help="GPU model name (e.g. 'NVIDIA H200 NVL').")
@click.option("--gpu-count", type=int, required=True)
@click.option(
    "--gpu-price",
    type=float,
    default=None,
    help="Override price-per-GPU/hour for the estimate (optional).",
)
@with_provider_overrides
@click.pass_context
def estimate_rewards(
    ctx: click.Context,
    gpu_type: str,
    gpu_count: int,
    gpu_price: float | None,
) -> None:
    require_hotkey(ctx, group="machine")
    client = build_client(ctx)
    params: dict[str, object] = {"gpu_type": gpu_type, "gpu_count": gpu_count}
    if gpu_price is not None:
        params["gpu_price"] = gpu_price
    try:
        body = client.estimated_rewards(**params)
    except ProviderError as e:
        ctx.exit(handle_provider_error(ctx, e))
        return
    render(ctx, body, summary=f"estimate: {gpu_count}x{gpu_type}")


__all__ = [
    "billing_command",
    "machine_command",
    "machine_request_command",
]
