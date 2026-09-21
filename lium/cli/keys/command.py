"""`lium keys`: API keys of a workspace (session-only on the server: a key cannot mint keys)."""

import json
from typing import Dict, List, Optional

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.settings import config as settings
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, format_date, handle_errors
from lium.cli.workspaces.command import require_session, section_for, target_workspace, workspace_client
from lium.sdk import ApiKeyInfo, ApiKeyScope, Lium
from lium.sdk.api_keys import BILLING_SCOPE, DEFAULT_SCOPES, POD_VISIBILITIES
from lium.sdk.exceptions import LiumError

# The scopes the option accepts. The server's list (`lium keys scopes`) is the source of what each one does; this
# is only what the CLI lets you type. `billing` (the money routes: card payments, credit transfers, crypto
# payments) is never in the default set — lium-platform P235, not released.
SCOPE_CHOICES = (*DEFAULT_SCOPES, BILLING_SCOPE)


def _usd(value: Optional[float]) -> str:
    return "—" if value is None else f"${value:,.2f}"


def budget_cell(key: ApiKeyInfo) -> str:
    """`today/day · total/max`: spent over budget for each window the key has; `—` for a key with none.

    A server before P235 sends no budget fields at all, so every key reads `—` there.
    """
    parts: List[str] = []
    if key.daily_budget_usd is not None:
        parts.append(f"{_usd(key.spent_today_usd)}/{_usd(key.daily_budget_usd)} today")
    if key.max_budget_usd is not None:
        parts.append(f"{_usd(key.spent_total_usd)}/{_usd(key.max_budget_usd)} total")
    return " · ".join(parts) or "—"


def _pods_by_key(lium: Lium) -> Optional[Dict[str, int]]:
    """Active pods per API key id, from one `GET /pods` with the account's key; None when that read fails
    (a key without `read`, an older server) — the column then shows `—` rather than a wrong zero."""
    try:
        pods = lium.ps()
    except Exception as exc:  # noqa: BLE001 - the count is decoration; the key rows are still worth showing
        ui.notice_debug(f"pod count unavailable: {exc}")
        return None
    counts: Dict[str, int] = {}
    for pod in pods:
        if pod.api_key_id:
            counts[pod.api_key_id] = counts.get(pod.api_key_id, 0) + 1
    return counts


def _scopes_by_name(lium: Lium) -> Dict[str, ApiKeyScope]:
    return {scope.scope: scope for scope in lium.api_keys.scopes()}


def _can_do_lines(key: ApiKeyInfo, scopes: Dict[str, ApiKeyScope]) -> List[str]:
    """"What this key can do": the server's description for each scope the key holds, in the key's order;
    a scope the server does not describe is named alone."""
    lines = []
    for name in key.scopes:
        scope = scopes.get(name)
        lines.append(f"{name}: {scope.description}" if scope and scope.description else name)
    return lines


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

    Columns: Name, Scopes, Budget (spent/budget for the day and in total, when the key has one), Pods (active
    pods rented through the key, counted from `lium ps`), Created, Last used, ID. Budgets and the Pods count
    need a server with per-key budgets (lium-platform P235, not released); older servers show `—`.
    """
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    keys = lium.api_keys.list(target.id)
    if json_output:
        click.echo(json.dumps([key.to_dict() for key in keys], indent=2))
        return
    pods = _pods_by_key(lium) if keys else {}
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Name", "Scopes", "Budget", "Pods", "Created", "Last used", "ID"):
        table.add_column(column, overflow="fold")
    for key in keys:
        table.add_row(
            Text(key.name),
            ",".join(key.scopes) or "—",
            budget_cell(key),
            "—" if pods is None else str(pods.get(key.id, 0)),
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
    "--daily-budget", type=click.FloatRange(min=0, min_open=True), metavar="USD",
    help="Refuse new rentals through this key once its pods were billed this much in a UTC day "
         "(lium-platform P235, not released)",
)
@click.option(
    "--max-budget", type=click.FloatRange(min=0, min_open=True), metavar="USD",
    help="Refuse new rentals through this key once its pods were billed this much in total "
         "(lium-platform P235, not released)",
)
@click.option(
    "--pod-visibility", type=click.Choice(POD_VISIBILITIES), default="own", show_default=True,
    help="Which pods the key lists and manages: own = only the pods it rented; account = every pod of the "
         "account (lium-platform P235, not released)",
)
@click.option("--workspace", "-w", default=None, help="Workspace name or id the key is bound to (default: current)")
@click.option("--save", is_flag=True, help="Keep the key in ~/.lium/config.ini for `--workspace <name>`")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def keys_create_command(
    name: str,
    scopes: tuple,
    daily_budget: Optional[float],
    max_budget: Optional[float],
    pod_visibility: str,
    workspace: Optional[str],
    save: bool,
    json_output: bool,
):
    """Create an API key bound to a workspace; the key is printed once.

    Without --scope the key gets read, rent and manage. `--scope billing` adds the money routes (card
    payments, credit transfers, crypto payments) and prints the server's warning first; it is never
    added on its own. Budgets are USD; when one is reached, a rent through the key is refused with
    `budget_exceeded` (exit 6) while the running pods keep running.

    \b
    Examples:
      lium keys create agent-1 --scope read --scope rent --daily-budget 20 --max-budget 200
      lium keys create ops --scope read --scope rent --scope manage --pod-visibility account
      lium keys create ci --workspace Research --save
    """
    if daily_budget is not None and max_budget is not None and max_budget < daily_budget:
        raise CliFailure(
            "invalid_arguments",
            f"--max-budget ({_usd(max_budget)}) is below --daily-budget ({_usd(daily_budget)}); "
            "the total budget cannot be smaller than one day's",
            EXIT_CONFIGURATION_ERROR,
        )
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    # a name config.ini cannot hold, or a section already holding another same-named workspace's key, is refused before minting
    section = section_for(target) if save else None
    chosen = list(dict.fromkeys(scopes)) or list(DEFAULT_SCOPES)
    if BILLING_SCOPE in chosen:
        _warn_billing(lium, json_output)
    key = lium.api_keys.create(
        name,
        chosen,
        daily_budget_usd=daily_budget,
        max_budget_usd=max_budget,
        pod_visibility=pod_visibility,
        workspace_id=target.id,
    )
    if section:
        settings.set_in_section(section, "id", target.id)
        settings.set_in_section(section, "api_key", key.key or "")
    if json_output:
        click.echo(json.dumps({**key.raw, "workspace_name": target.name}, indent=2))
        return
    ui.success(f"Key '{escape(name)}' created in {escape(target.name)}")
    ui.print(Text(key.key or ""))
    ui.dim(f"Scopes: {', '.join(key.scopes) or ', '.join(chosen)}" + (f"  ·  Budget: {budget_cell(key)}" if budget_cell(key) != "—" else ""))
    ui.dim(
        f"Saved for `lium --workspace {escape(target.name)} …`" if save else "Not saved; add --save to use it with --workspace"
    )


def _warn_billing(lium: Lium, json_output: bool) -> None:
    """One line before minting a key that holds `billing`: the server's own description of the scope (the
    words come from `GET /keys/scopes`, not from here). On a server without that route the line names the
    scope and says the description is unavailable. Under --json the line goes to stderr."""
    try:
        description = _scopes_by_name(lium).get(BILLING_SCOPE)
        text = description.description if description and description.description else ""
    except LiumError as exc:
        ui.notice_debug(f"scopes unavailable: {exc}")
        text = ""
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
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the row plus `can_do` and `pods`)")
@handle_errors
def keys_show_command(key: str, workspace: Optional[str], json_output: bool):
    """One key by name or id: its scopes with the server's description of each, budget, visibility and pods.

    "What this key can do" is read from the server (`GET /keys/scopes`, lium-platform P235, not released);
    on an older server the scope names are listed alone.

    \b
    Examples:
      lium keys show agent-1
      lium keys show 5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d --json
    """
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    found = lium.api_keys.resolve(key, target.id)
    try:
        scopes = _scopes_by_name(lium)
    except LiumError as exc:
        ui.notice_debug(f"scopes unavailable: {exc}")
        scopes = {}
    can_do = _can_do_lines(found, scopes)
    pods = _pods_by_key(lium)
    pod_count = None if pods is None else pods.get(found.id, 0)
    if json_output:
        click.echo(json.dumps({**found.to_dict(), "can_do": can_do, "pods": pod_count}, indent=2))
        return
    ui.info(f"{escape(found.name)}  ({found.id})")
    rows = [
        ("Workspace", target.name),
        ("Scopes", ", ".join(found.scopes) or "—"),
        ("Budget", budget_cell(found)),
        ("Pod visibility", found.pod_visibility or "—"),
        ("Active pods", "—" if pod_count is None else str(pod_count)),
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
    if pod_count:
        ui.dim(f"lium ps --key {found.name} lists its pods; lium billing history --key {found.name} its charges")


@keys_command.command("scopes")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the server's rows)")
@handle_errors
def keys_scopes_command(json_output: bool):
    """Every API-key scope with what it allows, in the server's words (`GET /keys/scopes`).

    Needs a server with the route (lium-platform P235, not released); older servers answer `not_found`.

    \b
    Examples:
      lium keys scopes
      lium keys scopes --json | jq '.[].scope'
    """
    lium = workspace_client()
    scopes = lium.api_keys.scopes()
    if json_output:
        click.echo(json.dumps(
            [{"scope": s.scope, "description": s.description, "route_families": s.route_families} for s in scopes],
            indent=2,
        ))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Scope", "What it allows", "Routes"):
        table.add_column(column, overflow="fold")
    for scope in scopes:
        table.add_row(Text(scope.scope), Text(scope.description or "—"), ", ".join(scope.route_families) or "—")
    ui.info(f"API key scopes  ({len(scopes)} total)")
    ui.print(table)
    ui.dim(f"A key made without --scope gets {', '.join(DEFAULT_SCOPES)}; {BILLING_SCOPE} only when asked for")
