"""`lium keys`: API keys of a workspace (session-only on the server: a key cannot mint keys)."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.settings import config as settings
from lium.cli.utils import CliFailure, EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, format_date, handle_errors
from lium.cli.workspaces.command import require_session, section_for, target_workspace, workspace_client
from lium.sdk import ApiKeyInfo, ApiKeyRefusal, ApiKeyScope, Lium
from lium.sdk.api_keys import (
    BILLING_SCOPE,
    BUDGET_FIELDS,
    BUDGET_MIN_USD,
    DEFAULT_SCOPES,
    NO_BUDGETS,
    POD_VISIBILITIES,
    UNSET,
    check_budget_order,
    check_scopes,
    parse_stamp,
    unrecorded,
)
from lium.sdk.exceptions import LiumError, LiumNotFoundError

# The scopes the option accepts. The server's list (`lium keys scopes`) is the source of what each one does; this
# is only what the CLI lets you type. `billing` (the money routes: card payments, credit transfers, crypto
# payments) is never in the default set; a server before per-key budgets knows the first three only.
SCOPE_CHOICES = (*DEFAULT_SCOPES, BILLING_SCOPE)


def _usd(value: Optional[float]) -> str:
    return "—" if value is None else f"${value:,.2f}"


def budget_cell(key: ApiKeyInfo) -> str:
    """`today/day · month/monthly · total/max`: spent over budget for each window the key has; `—` for a key
    with none.

    A server before per-key budgets sends no budget fields at all, so every key reads `—` there.
    """
    parts: List[str] = []
    if key.daily_budget_usd is not None:
        parts.append(f"{_usd(key.spent_today_usd)}/{_usd(key.daily_budget_usd)} today")
    if key.monthly_budget_usd is not None:
        parts.append(f"{_usd(key.spent_month_usd)}/{_usd(key.monthly_budget_usd)} month")
    if key.max_budget_usd is not None:
        parts.append(f"{_usd(key.spent_total_usd)}/{_usd(key.max_budget_usd)} total")
    return " · ".join(parts) or "—"


def _pods_cell(key: ApiKeyInfo) -> str:
    """Active pods the key created, as the server counts them (`pods_count`); `—` from a server before per-key budgets."""
    return "—" if key.pods_count is None else str(key.pods_count)


def _scopes_by_name(lium: Lium) -> Dict[str, ApiKeyScope]:
    """The server's scope table; empty on a server without `GET /keys/scopes` (the names are then shown alone)."""
    try:
        return {scope.scope: scope for scope in lium.api_keys.scopes()}
    except LiumError as exc:
        ui.notice_debug(f"scopes unavailable: {exc}")
        return {}


def _can_do_lines(key: ApiKeyInfo, scopes: Dict[str, ApiKeyScope]) -> List[str]:
    """"What this key can do": the server's `can` lines (its one-sentence description when it sent none) for
    each scope the key holds, in the key's order; a scope the server does not describe is named alone."""
    lines = []
    for name in key.scopes:
        scope = scopes.get(name)
        if scope and scope.can:
            lines.extend(f"{name}: {line}" for line in scope.can)
        elif scope and scope.description:
            lines.append(f"{name}: {scope.description}")
        else:
            lines.append(name)
    return lines


BUDGET_RANGE = click.FloatRange(min=BUDGET_MIN_USD)


@click.group("keys", invoke_without_command=True)
@click.pass_context
def keys_command(ctx):
    """API keys, per workspace. Needs `lium workspaces login` first (keys cannot manage keys).

    \b
    Examples:
      lium keys                                          # the keys of the current workspace
      lium keys create agent-1 --scope read --scope rent --daily-budget 20
      lium keys show agent-1                             # what the key can do, its budget, its pods
      lium keys scopes                                   # every scope, in the server's words
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(keys_list_command)


@keys_command.command("list")
@click.option("--workspace", "-w", default=None, help="Workspace name or id (default: the current one)")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the rows without the key material)")
@handle_errors
def keys_list_command(workspace: Optional[str], json_output: bool):
    """List the API keys of a workspace (owners and admins); a key's secret is printed once, by `keys create`.

    Columns: Name, Scopes, Budget (spent/budget for the day, the month and in total, when the key has one), Pods (active
    pods the key created), Created, Last used, ID. Budgets and the Pods count come from a server with per-key
    budgets (not on lium.io yet); older servers show `—`.
    """
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    keys = lium.api_keys.list(target.id)
    if json_output:
        click.echo(json.dumps([key.to_dict() for key in keys], indent=2))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Name", "Scopes", "Budget", "Pods", "Created", "Last used", "ID"):
        table.add_column(column, overflow="fold")
    for key in keys:
        table.add_row(
            Text(key.name),
            ",".join(key.scopes) or "—",
            budget_cell(key),
            _pods_cell(key),
            format_date(key.created_at) if key.created_at else "—",
            format_date(key.last_used) if key.last_used else "—",
            key.id,
        )
    ui.info(f"API keys of {escape(target.name)}  ({len(keys)} total)")
    ui.print(table)


@keys_command.command("create")
@click.argument("name")
@click.option(
    "--scope", "scopes", multiple=True, type=click.Choice(SCOPE_CHOICES),
    help=f"A scope the key holds; repeatable. Default: {', '.join(DEFAULT_SCOPES)} — never {BILLING_SCOPE}. "
         "`lium keys scopes` says what each one allows.",
)
@click.option(
    "--daily-budget", type=BUDGET_RANGE, metavar="USD",
    help="Most USD the pods this key creates may be billed on one UTC day; at it the key's pods are stopped and "
         "new rentals through the key are refused. At least $1, whole cents (needs a server with per-key budgets; "
         "not on lium.io yet)",
)
@click.option(
    "--monthly-budget", type=BUDGET_RANGE, metavar="USD",
    help="The same over one UTC calendar month (needs a server with per-key budgets; not on lium.io yet)",
)
@click.option(
    "--max-budget", type=BUDGET_RANGE, metavar="USD",
    help="The same over the key's lifetime (needs a server with per-key budgets; not on lium.io yet)",
)
@click.option(
    "--pod-visibility", type=click.Choice(POD_VISIBILITIES), default=None,
    help="own = only the pods this key creates exist for it; account = every pod of the account, within the "
         "key's scopes. Not passed: the server's own default decides (needs a server with pod visibility; "
         "not on lium.io yet)",
)
@click.option(
    "--allow-unbudgeted", is_flag=True,
    help="Keep the key even when the server did not record a budget or visibility asked for (a warning instead "
         "of a refusal)",
)
@click.option("--workspace", "-w", default=None, help="Workspace name or id the key is bound to (default: current)")
@click.option("--save", is_flag=True, help="Keep the key in ~/.lium/config.ini for `--workspace <name>`")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def keys_create_command(
    name: str,
    scopes: tuple,
    daily_budget: Optional[float],
    monthly_budget: Optional[float],
    max_budget: Optional[float],
    pod_visibility: Optional[str],
    allow_unbudgeted: bool,
    workspace: Optional[str],
    save: bool,
    json_output: bool,
):
    """Create an API key bound to a workspace; the key is printed once.

    Without --scope the key gets read, rent and manage. `--scope billing` gives the money routes (card
    payments, credit transfers, crypto top-ups) and nothing else: it is never added on its own, it prints the
    server's warning first, and it cannot be combined with another scope (refused here, before any request).
    Budgets are USD per window — day, month, lifetime; at one, the server stops the key's pods and refuses a
    rent, a pod extend or a top-up through it with `API_KEY_BUDGET_EXCEEDED` (exit 6), naming the window hit.
    `lium keys budget` changes them later. A server without per-key budgets (lium.io today) cannot record a
    budget or a pod visibility: the CLI then refuses to create the key (exit 2; a key the server minted
    uncapped is revoked) unless --allow-unbudgeted is passed.

    \b
    Examples:
      lium keys create agent-1 --scope read --scope rent --daily-budget 20 --monthly-budget 300 --max-budget 2000
      lium keys create ops --scope read --scope rent --scope manage --pod-visibility account
      lium keys create payer --scope billing            # money routes only; warns first
      lium keys create ci --workspace Research --save
    """
    chosen = list(dict.fromkeys(scopes)) or list(DEFAULT_SCOPES)
    try:
        check_scopes(chosen)
        check_budget_order(daily_budget, monthly_budget, max_budget)
    except ValueError as exc:
        raise CliFailure("invalid_arguments", str(exc), EXIT_CONFIGURATION_ERROR) from exc
    asked = {
        "daily_budget_usd": daily_budget,
        "monthly_budget_usd": monthly_budget,
        "max_budget_usd": max_budget,
        "pod_visibility": pod_visibility,
    }
    lium = workspace_client()
    require_session(lium)
    if any(value is not None for value in asked.values()) and not allow_unbudgeted:
        _require_budget_support(lium, asked)
    target = target_workspace(lium, workspace)
    # a name config.ini cannot hold, or a section already holding another same-named workspace's key, is refused before minting
    section = section_for(target) if save else None
    if BILLING_SCOPE in chosen:
        _warn_billing(lium, json_output)
    key = lium.api_keys.create(
        name,
        chosen,
        daily_budget_usd=daily_budget,
        monthly_budget_usd=monthly_budget,
        max_budget_usd=max_budget,
        pod_visibility=pod_visibility,
        workspace_id=target.id,
    )
    missing = unrecorded(key, asked)
    if missing and not allow_unbudgeted:
        _refuse_unrecorded(lium, key, asked, missing, target.id)
    if section:
        settings.set_in_section(section, "id", target.id)
        settings.set_in_section(section, "api_key", key.key or "")
    if missing:
        _warn_unrecorded(asked, missing, json_output)
    if json_output:
        click.echo(json.dumps({**key.raw, "workspace_name": target.name}, indent=2))
        return
    ui.success(f"Key '{escape(name)}' created in {escape(target.name)}")
    ui.print(Text(key.key or ""))
    budget = budget_cell(key)
    ui.dim(f"Scopes: {', '.join(key.scopes) or ', '.join(chosen)}" + (f"  ·  Budget: {budget}" if budget != "—" else ""))
    ui.dim(
        f"Saved for `lium --workspace {escape(target.name)} …`" if save else "Not saved; add --save to use it with --workspace"
    )


def _flags(asked: Dict[str, Any], fields: List[str]) -> List[str]:
    """`--daily-budget $20.00`, `--pod-visibility own` — the flags behind the fields named, as the user typed them."""
    words = []
    for field_name in fields:
        value = asked[field_name]
        words.append(f"{_flag(field_name)} {_usd(value) if field_name in BUDGET_FIELDS else value}")
    return words


def _flag(field_name: str) -> str:
    return "--" + field_name.removesuffix("_usd").replace("_", "-")


def _without(fields: List[str]) -> str:
    return " and ".join(_flag(field_name) for field_name in fields)


def _require_budget_support(lium: Lium, asked: Dict[str, Any]) -> None:
    """Before minting a key with a budget or a pod visibility: a server without `GET /keys/scopes` has none of
    them (lium.io on 21 Sep 2026) and would mint the key uncapped — refuse first, so nothing is created."""
    try:
        lium.api_keys.scopes_payload()
    except LiumNotFoundError as exc:
        wanted = [field_name for field_name, value in asked.items() if value is not None]
        raise CliFailure(
            "invalid_arguments",
            f"{NO_BUDGETS} (no GET /keys/scopes): create the key without {_without(wanted)}, or upgrade the server",
            EXIT_CONFIGURATION_ERROR,
            hint="Pass --allow-unbudgeted to mint the key anyway, with no cap and the server's default pod visibility",
        ) from exc


def _refuse_unrecorded(lium: Lium, key: ApiKeyInfo, asked: Dict[str, Any], missing: List[str], workspace_id: str) -> None:
    """The server minted the key but recorded none of `missing` (its request model dropped the fields it does
    not know): a key that exists with NO cap is exactly what the caller must not be handed believing it is
    capped — revoke it and refuse (exit 2). When the revoke itself fails the key is named so it can be
    revoked by hand."""
    verb = "were" if len(missing) > 1 else "was"
    what = f"{' and '.join(_flags(asked, missing))} {verb} not recorded"
    try:
        lium.api_keys.revoke(key.id, workspace_id)
        outcome = f"the key '{key.name}' was minted uncapped and has been revoked"
    except LiumError as exc:
        outcome = f"the key '{key.name}' ({key.id}) was minted uncapped and could NOT be revoked ({exc}); revoke it in the dashboard"
    raise CliFailure(
        "invalid_arguments",
        f"{NO_BUDGETS}: {what} — {outcome}. Create the key without {_without(missing)}, or upgrade the server",
        EXIT_CONFIGURATION_ERROR,
        data={"unrecorded": missing, "key_id": key.id},
        hint="Pass --allow-unbudgeted to keep such a key, with a warning instead of this refusal",
    )


def _warn_unrecorded(asked: Dict[str, Any], missing: List[str], json_output: bool) -> None:
    """--allow-unbudgeted: the key is kept and printed, and what the server did not record is said once, so
    nobody trusts a cap that was never set. Under --json the line goes to stderr."""
    verb = "were" if len(missing) > 1 else "was"
    line = (
        f"Warning: {NO_BUDGETS[0].lower()}{NO_BUDGETS[1:]}: {' and '.join(_flags(asked, missing))} {verb} not recorded — "
        "the key has no such cap; it sees the pods the server's default allows"
    )
    if json_output:
        click.echo(line, err=True)
    else:
        ui.warning(escape(line))


def _warn_billing(lium: Lium, json_output: bool) -> None:
    """One line before minting a key that holds `billing`: the server's own description of the scope (the
    words come from `GET /keys/scopes`, not from here). On a server without that route the line names the
    scope and says the description is unavailable. Under --json the line goes to stderr."""
    description = _scopes_by_name(lium).get(BILLING_SCOPE)
    text = description.description if description and description.description else ""
    line = f"Warning: this key holds the '{BILLING_SCOPE}' scope — " + (
        text or "the server did not describe it (no GET /keys/scopes); it is the elevated, money-moving scope"
    )
    if json_output:
        click.echo(line, err=True)
    else:
        ui.warning(escape(line))


@keys_command.command("show")
@click.argument("key")
@click.option("--workspace", "-w", default=None, help="Workspace name or id (default: the current one)")
@click.option(
    "--json", "json_output", is_flag=True,
    help="Machine-readable output (the row, `pods_count` included, plus `can_do`, `refusals` and `refusals_today`)",
)
@handle_errors
def keys_show_command(key: str, workspace: Optional[str], json_output: bool):
    """One key by name or id: what it can do, spent/budget per window, its pod visibility, pod count and the
    last requests its budget refused.

    "What this key can do" is the server's list for the scopes the key holds (`GET /keys/scopes`); the
    refusals are the server's ledger (`GET /keys/{id}/refusals`: when, which window, what was asked). Both
    Both need a newer Lium server (not on lium.io yet): an older server lists the scope names alone and has no
    refusal ledger.

    \b
    Examples:
      lium keys show agent-1
      lium keys show 5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d --json
    """
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    found = lium.api_keys.resolve(key, target.id)
    can_do = _can_do_lines(found, _scopes_by_name(lium))
    refusals = _refusals(lium, found.id, target.id)
    today = _refused_today(refusals) if refusals is not None else None
    if json_output:
        rows = None if refusals is None else [r.raw for r in refusals]
        click.echo(json.dumps({**found.to_dict(), "can_do": can_do, "refusals": rows, "refusals_today": today}, indent=2))
        return
    visibility = found.pod_visibility or "—"
    described = lium.api_keys.pod_visibilities().get(found.pod_visibility or "") if found.pod_visibility else None
    ui.info(f"{escape(found.name)}  ({found.id})")
    rows = [
        ("Workspace", target.name),
        ("Scopes", ", ".join(found.scopes) or "—"),
        ("Budget", budget_cell(found)),
        ("Pod visibility", f"{visibility} — {described}" if described else visibility),
        ("Active pods", _pods_cell(found)),
        ("Refused", _refused_cell(refusals, today)),
        ("Created", format_date(found.created_at) if found.created_at else "—"),
        ("Last used", format_date(found.last_used) if found.last_used else "—"),
    ]
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column(style="dim")
    table.add_column(overflow="fold")
    for label, value in rows:
        table.add_row(label, Text(value))
    ui.print(table)
    ui.print(Text("What this key can do:", style="bold"))
    for line in can_do or ["— (no scopes)"]:
        ui.print(Text(f"  • {line}"))
    ui.dim(f"lium ps --key {escape(found.name)} lists its pods; lium billing history --key {escape(found.name)} its charges")


def _refusals(lium: Lium, key_id: str, workspace_id: str) -> Optional[List[ApiKeyRefusal]]:
    """The key's refusal ledger, newest first; `None` on a server without `GET /keys/{id}/refusals` (404,
    server support pending) — shown as "no refusal ledger on this server", not as "none"."""
    try:
        return lium.api_keys.refusals(key_id, workspace_id)
    except LiumNotFoundError as exc:
        ui.notice_debug(f"refusals unavailable: {exc}")
        return None


def _today() -> str:
    """The UTC day the budgets are counted in (`YYYY-MM-DD`)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _refused_today(refusals: List[ApiKeyRefusal]) -> int:
    """Rows whose stamp falls on the current UTC day — the stamp parsed and normalised to UTC first, so a `Z`,
    an offset or a naive stamp count alike; an unreadable stamp does not count."""
    day = _today()
    stamps = (parse_stamp(r.at) for r in refusals)
    return sum(1 for stamp in stamps if stamp is not None and stamp.strftime("%Y-%m-%d") == day)


def _refused_cell(refusals: Optional[List[ApiKeyRefusal]], today: Optional[int]) -> str:
    """`3 today — last: rent $4.20, daily budget $20.00 reached` from the ledger's newest row."""
    if refusals is None:
        return "— (this server has no refusal ledger)"
    if not refusals:
        return "none"
    last = refusals[0]
    asked = f"{last.route or 'request'}" + (f" {_usd(last.amount_usd)}" if last.amount_usd is not None else "")
    window = f"{last.window} budget" if last.window else "budget"
    reached = f"{window} {_usd(last.budget_usd)} reached" if last.budget_usd is not None else f"{window} reached"
    when = f" ({format_date(last.at)})" if last.at else ""
    return f"{today or 0} today — last: {asked}, {reached}{when}"


@keys_command.command("scopes")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the server's body: scopes, pod_visibility, money_routes)")
@handle_errors
def keys_scopes_command(json_output: bool):
    """Every API-key scope and pod-visibility value with what it allows, in the server's words (`GET /keys/scopes`).

    Needs a server with the route (not on lium.io yet); older servers answer `not_found`. No
    session is needed: the table is static text, sent to any caller (the configured key goes along unchecked).

    \b
    Examples:
      lium keys scopes
      lium keys scopes --json | jq '.scopes[].scope'
    """
    lium = workspace_client()
    scopes = lium.api_keys.scopes()
    visibilities = lium.api_keys.pod_visibilities()
    if json_output:
        # the server's body as it came: `scopes`, `pod_visibility`, `money_routes`
        click.echo(json.dumps(lium.api_keys.scopes_payload(), indent=2))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Scope", "Default", "What it allows", "Routes"):
        table.add_column(column, overflow="fold")
    for scope in scopes:
        allows = scope.description or "—"
        if scope.can:
            allows += "\n" + "\n".join(f"• {line}" for line in scope.can)
        table.add_row(Text(scope.scope), "yes" if scope.default else "no", Text(allows), ", ".join(scope.route_families) or "—")
    ui.info(f"API key scopes  ({len(scopes)} total)")
    ui.print(table)
    if visibilities:
        ui.print(Text("Pod visibility (--pod-visibility):", style="bold"))
        for value, text in visibilities.items():
            ui.print(Text(f"  {value}: {text}"))
    defaults = [s.scope for s in scopes if s.default] or list(DEFAULT_SCOPES)
    ui.dim(f"A key made without --scope gets {', '.join(defaults)}; {BILLING_SCOPE} only when asked for")


@keys_command.command("budget")
@click.argument("key")
@click.option("--daily-budget", type=BUDGET_RANGE, metavar="USD", help="Set the per-UTC-day budget (at least $1, whole cents)")
@click.option("--monthly-budget", type=BUDGET_RANGE, metavar="USD", help="Set the per-UTC-month budget (at least $1, whole cents)")
@click.option("--max-budget", type=BUDGET_RANGE, metavar="USD", help="Set the lifetime budget (at least $1, whole cents)")
@click.option("--no-daily-budget", is_flag=True, help="Clear the per-day budget")
@click.option("--no-monthly-budget", is_flag=True, help="Clear the per-month budget")
@click.option("--no-max-budget", is_flag=True, help="Clear the lifetime budget")
@click.option("--workspace", "-w", default=None, help="Workspace name or id (default: the current one)")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the key's row after the change)")
@handle_errors
def keys_budget_command(
    key: str,
    daily_budget: Optional[float],
    monthly_budget: Optional[float],
    max_budget: Optional[float],
    no_daily_budget: bool,
    no_monthly_budget: bool,
    no_max_budget: bool,
    workspace: Optional[str],
    json_output: bool,
):
    """Set or clear a key's budgets — day, month, lifetime (`PATCH /keys/{id}`; needs a server with per-key
    budgets, not on lium.io yet — an older server has no such route and the CLI says so).

    A budget not named is left as it is; those named must keep daily ≤ monthly ≤ max; a budget the server did
    not record (a window it does not know) is refused, exit 2. Scopes and pod visibility cannot be changed after
    creation. Needs `lium workspaces login`: a key cannot lift its own budget.

    \b
    Examples:
      lium keys budget agent-1 --daily-budget 50 --monthly-budget 600
      lium keys budget agent-1 --no-max-budget
    """
    windows = (
        ("daily", daily_budget, no_daily_budget),
        ("monthly", monthly_budget, no_monthly_budget),
        ("max", max_budget, no_max_budget),
    )
    if any(value is not None and clear for _, value, clear in windows):
        raise CliFailure("invalid_arguments", "Set a budget or clear it, not both", EXIT_CONFIGURATION_ERROR)
    wanted = {name: (None if clear else (value if value is not None else UNSET)) for name, value, clear in windows}
    if all(value is UNSET for value in wanted.values()):
        raise CliFailure(
            "invalid_arguments",
            "Name what to change: --daily-budget / --no-daily-budget, --monthly-budget / --no-monthly-budget, "
            "--max-budget / --no-max-budget",
            EXIT_CONFIGURATION_ERROR,
        )
    try:
        check_budget_order(*(None if value is UNSET else value for value in wanted.values()))
    except ValueError as exc:
        raise CliFailure("invalid_arguments", str(exc), EXIT_CONFIGURATION_ERROR) from exc
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    found = lium.api_keys.resolve(key, target.id)
    try:
        updated = lium.api_keys.update(
            found.id,
            daily_budget_usd=wanted["daily"],
            monthly_budget_usd=wanted["monthly"],
            max_budget_usd=wanted["max"],
            workspace_id=target.id,
        )
    except LiumError as exc:
        if _no_patch_route(exc):
            raise CliFailure(
                "not_found",
                f"{NO_BUDGETS} (no PATCH /keys/{{id}}): the budget of '{found.name}' was not changed; upgrade the server",
                EXIT_API_ERROR,
                hint="`lium keys show` on such a server shows no budget row to change",
            ) from exc
        raise
    set_fields = {
        field_name: value for field_name, value in zip(BUDGET_FIELDS, wanted.values(), strict=True) if value not in (None, UNSET)
    }
    missing = unrecorded(updated, set_fields)
    if missing:
        verb = "were" if len(missing) > 1 else "was"
        raise CliFailure(
            "invalid_arguments",
            f"This server does not know this budget window yet: {' and '.join(_flags(set_fields, missing))} {verb} not "
            f"recorded — the budget of '{updated.name}' reads {budget_cell(updated)}; upgrade the server",
            EXIT_CONFIGURATION_ERROR,
            data={"unrecorded": missing},
        )
    if json_output:
        click.echo(json.dumps(updated.to_dict(), indent=2))
        return
    ui.success(f"Budget of '{escape(updated.name)}' is now {budget_cell(updated)}")


def _no_patch_route(exc: LiumError) -> bool:
    """A server before per-key budgets has no `PATCH /keys/{id}`: 405 (the path exists for GET/PUT/DELETE) or 404."""
    return isinstance(exc, LiumNotFoundError) or str(exc).startswith("API error 405")
