"""`lium keys`: API keys of a workspace (session-only on the server: a key cannot mint keys)."""

import json
from typing import Optional

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.settings import config as settings
from lium.cli.utils import format_date, handle_errors
from lium.cli.workspaces.command import require_session, section_for, target_workspace, workspace_client


@click.group("keys", invoke_without_command=True)
@click.pass_context
def keys_command(ctx):
    """API keys, per workspace. Needs `lium workspaces login` first (keys cannot manage keys).

    \b
    Examples:
      lium keys                                   # the current workspace's keys
      lium keys list --workspace research --json
      lium keys create ci --workspace research --save
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(keys_list_command)


@keys_command.command("list")
@click.option("--workspace", "-w", default=None, help="Workspace name or id (default: the current one)")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (the rows without the key material)")
@handle_errors
def keys_list_command(workspace: Optional[str], json_output: bool):
    """List the API keys of a workspace (owners and admins); a key's secret is printed once, by `keys create`."""
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    keys = lium.workspaces.list_keys(target.id)
    if json_output:
        click.echo(json.dumps([{k: v for k, v in key.items() if k != "key"} for key in keys], indent=2))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Name", "Scopes", "Created", "Last used", "ID"):
        table.add_column(column, overflow="fold")
    for key in keys:
        table.add_row(
            Text(key.get("name", "")),
            ",".join(key.get("scopes") or []) or "—",
            format_date(key["created_at"]) if key.get("created_at") else "—",
            format_date(key["last_used"]) if key.get("last_used") else "—",
            key.get("id", ""),
        )
    ui.info(f"API keys of {escape(target.name)}  ({len(keys)} total)")
    ui.print(table)


@keys_command.command("create")
@click.argument("name")
@click.option("--workspace", "-w", default=None, help="Workspace name or id the key is bound to (default: current)")
@click.option("--save", is_flag=True, help="Keep the key in ~/.lium/config.ini for `--workspace <name>`")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def keys_create_command(name: str, workspace: Optional[str], save: bool, json_output: bool):
    """Create an API key bound to a workspace; the key is printed once."""
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    # a name config.ini cannot hold, or a section already holding another same-named workspace's key, is refused before minting
    section = section_for(target) if save else None
    key = lium.workspaces.create_key(name, target.id)
    if section:
        settings.set_in_section(section, "id", target.id)
        settings.set_in_section(section, "api_key", key.get("key", ""))
    if json_output:
        click.echo(json.dumps({**key, "workspace_name": target.name}, indent=2))
        return
    ui.success(f"Key '{escape(name)}' created in {escape(target.name)}")
    ui.print(Text(key.get("key", "")))
    ui.dim(
        f"Saved for `lium --workspace {escape(target.name)} …`" if save else "Not saved; add --save to use it with --workspace"
    )
