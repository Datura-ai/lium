"""`lium workspaces`: the team a key acts in, and — with a browser session — the teams you belong to.

A key is bound to one workspace (lium-platform DAH-2986), so `--workspace` chooses which configured key
a command runs with; the server reports the key's workspace on GET /users/me (DAH-3030) and that field is
also how the CLI knows the server has workspaces at all. Reshaping a team is session-only on the server:
`lium workspaces login` keeps a session token for those subcommands.
"""

import json
import sys
from typing import Optional

import click
from rich.markup import escape

from lium.sdk import Config, Lium
from lium.sdk.config import workspace_section
from lium.sdk.exceptions import LiumSessionError
from lium.sdk.models import WorkspaceInfo
from lium.sdk.workspaces import NEEDS_SESSION
from lium.cli import ui
from lium.cli.interactive import is_interactive, noninteractive_reason
from lium.cli.settings import config as settings
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, ensure_config, handle_errors

from .display import build_members_table, build_workspaces_table, member_json, workspace_json

ROLES = click.Choice(["member", "admin", "owner"])
WORKSPACE_ARG = click.argument("workspace", required=False)


def workspace_client() -> Lium:
    """The SDK client for `lium workspaces` and `lium keys`; the config prompts run first.

    These commands manage teams with the account's key and a session: `--workspace` / LIUM_WORKSPACE
    names their default target but does not pick their key, so `lium keys create … --save` and
    `LIUM_API_KEY=… lium workspaces use …` work when no key is saved for that workspace yet.
    """
    ensure_config()
    return Lium(config=Config.load(key_for_workspace=False), source="cli")


def target_workspace(lium: Lium, workspace: Optional[str]) -> WorkspaceInfo:
    """The workspace named on the command line, else the configured one, else the key's own.

    A name with a saved `[workspace.<name>] id` is looked up by that id first, so a workspace renamed
    on the server is still found under the name it was saved as; the current name is the fallback.
    """
    name = workspace or lium.config.workspace
    if not name:
        return lium.workspaces.require_enabled()
    saved_id = _saved_id(name)
    if saved_id:
        lium.workspaces.require_enabled()
        found = [w for w in lium.workspaces.list() if w.id == saved_id]
        if found:
            return found[0]
    return lium.workspaces.resolve(name)


def require_session(lium: Lium) -> None:
    """The session-only subcommands stop here, before any request, when there is no session token."""
    if not lium.workspaces.session_token:
        raise LiumSessionError(NEEDS_SESSION)


def _member_id(lium: Lium, workspace: WorkspaceInfo, who: str) -> str:
    """A member's user id from an id or an e-mail, looked up in the member list."""
    for member in lium.workspaces.members(workspace.id):
        if who == member.user_id or (member.email and who.lower() == member.email.lower()):
            return member.user_id
    raise CliFailure("member_not_found", f"No member '{who}' in {workspace.name}", EXIT_CONFIGURATION_ERROR)


def section_for(workspace: WorkspaceInfo) -> str:
    """The `[workspace.<name>]` section a key for ``workspace`` may be saved in.

    Sections are keyed by the lower-cased name, so a second workspace with the same name (or the same
    name in another case) would land in the first one's section and overwrite its saved key without a
    word. A section that already holds another workspace's id is refused before anything is minted or
    written; a name config.ini cannot hold is refused by ``workspace_section`` the same way.
    """
    return _free_section(workspace.name, workspace.id)


def _free_section(name: str, workspace_id: Optional[str]) -> str:
    """``section_for`` by name: with no id yet (`workspaces create --use`, before the POST) any held id is another workspace's."""
    section = workspace_section(name)
    held = settings.get_in_section(section, "id")
    if held and held != workspace_id:
        raise CliFailure(
            "workspace_section_taken",
            f"config.ini already holds another workspace named '{name}' (id {held}); "
            f"drop its [{section}] section from ~/.lium/config.ini, or rename one of the two on lium.io, first",
            EXIT_CONFIGURATION_ERROR,
        )
    return section


def _remember(workspace: WorkspaceInfo, api_key: Optional[str] = None) -> None:
    """`[workspaces] active`, and — with a key — the `[workspace.<name>]` section (id and key) for `--workspace`."""
    section = section_for(workspace)  # refuses a name config.ini cannot hold, or a section another workspace holds, before anything is written
    settings.set("workspaces.active", workspace.name)
    if api_key:
        settings.set_in_section(section, "id", workspace.id)
        settings.set_in_section(section, "api_key", api_key)


def _saved_key(workspace: WorkspaceInfo) -> Optional[str]:
    return settings.get_in_section(workspace_section(workspace.name), "api_key")


def _saved_id(name: str) -> Optional[str]:
    try:
        return settings.get_in_section(workspace_section(name), "id")
    except ValueError:  # a name config.ini cannot hold has no section
        return None


def _forget(workspace: WorkspaceInfo) -> None:
    """Drop every `[workspace.<name>]` section saved for this workspace — by id, or by its current name — and
    the `[workspaces] active` default when it names one of them; a deleted workspace's key is dead."""
    names = {section[len("workspace."):] for section in settings.sections("workspace.")}
    gone = {name for name in names if settings.get_in_section(f"workspace.{name}", "id") == workspace.id}
    # The name-keyed section goes only when it is this workspace's (or carries no id): a same-named
    # workspace's saved id and key stay (the twin the `section_for` guard exists for).
    if _saved_id(workspace.name) in (None, workspace.id):
        gone.add(workspace.name.lower())
    for section in settings.sections("workspace."):
        if section[len("workspace."):] in gone:
            settings.remove_section(section)
    active = settings.get("workspaces.active")
    if active and active.lower() in gone:
        settings.unset("workspaces.active")


@click.group("workspaces", invoke_without_command=True)
@click.pass_context
def workspaces_command(ctx):
    """Teams: list the workspaces you belong to, pick one, manage members and billing.

    \b
      lium workspaces                      # the workspace this key acts in (all of yours with a session)
      lium workspaces login                # sign in once for create / invite / remove / transfer / delete
      lium workspaces use research         # make 'research' the default for every command
      lium --workspace research ps         # one command in another workspace (its key must be configured)
      lium keys create ci --workspace research --save   # a key for that workspace, saved for --workspace
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(workspaces_list_command)


@workspaces_command.command("list")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def workspaces_list_command(json_output: bool):
    """List workspaces: the one this key acts in, or every one you belong to with a session."""
    lium = workspace_client()
    current = lium.workspaces.require_enabled()
    workspaces = ui.load("Loading workspaces", lium.workspaces.list) if not json_output else lium.workspaces.list()
    if json_output:
        click.echo(json.dumps([workspace_json(w) for w in workspaces], indent=2))
        return
    active = settings.get("workspaces.active")
    table, header = build_workspaces_table(workspaces, current.id, active, _saved_id(active) if active else None)
    ui.info(header)
    ui.print(table)
    ui.dim("* = this key acts here" + ("   → = the default from `lium workspaces use`" if active else ""))
    if not lium.workspaces.session_token:
        ui.dim("A key sees its own workspace only; `lium workspaces login` lists every workspace you belong to")


@workspaces_command.command("members")
@WORKSPACE_ARG
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output")
@handle_errors
def workspaces_members_command(workspace: Optional[str], json_output: bool):
    """List the members of a workspace (default: the one this key acts in)."""
    lium = workspace_client()
    target = target_workspace(lium, workspace)
    members = lium.workspaces.members(target.id)
    if json_output:
        click.echo(json.dumps([member_json(m) for m in members], indent=2))
        return
    table, header = build_members_table(target, members)
    ui.info(header)
    ui.print(table)


@workspaces_command.command("use")
@click.argument("workspace")
@handle_errors
def workspaces_use_command(workspace: str):
    """Make WORKSPACE the default for every command; stored in ~/.lium/config.ini.

    A key acts in exactly one workspace, so the default only takes effect for commands that run with a
    key saved for it. The key this command runs with is saved when it acts in WORKSPACE (GET /users/me
    says so): `LIUM_API_KEY=<a key bound to WORKSPACE> lium workspaces use WORKSPACE` saves that key —
    the environment, not an argument, so the key is not in the process arguments (`ps`) — and
    `lium keys create <name> --workspace WORKSPACE --save` mints and saves one.
    """
    lium = workspace_client()
    target = lium.workspaces.resolve(workspace)
    current = lium.workspaces.current()
    acts_here = current is not None and current.id == target.id
    _remember(target, lium.config.api_key if acts_here else None)
    if acts_here:
        ui.success(f"Default workspace: {escape(target.name)} ({target.role}); this key is saved for `--workspace`")
        return
    ui.success(f"Default workspace: {escape(target.name)} ({target.role})")
    ran_with = f"the key this command ran with acts in {escape(current.name) if current else 'another workspace'}"
    if _saved_key(target):
        ui.dim(f"A key for {escape(target.name)} is already saved and will be used; {ran_with}")
        return
    ui.warning(
        f"No API key is saved for {escape(target.name)}; {ran_with}. "
        f"Run `lium keys create <name> --workspace {escape(target.name)} --save`, "
        f"or `LIUM_API_KEY=<a key bound to {escape(target.name)}> lium workspaces use {escape(target.name)}`."
    )


@workspaces_command.command("login")
@click.option("--email", help="The e-mail of your Lium account (asked for on a terminal when omitted)")
@click.option(
    "--password-stdin", is_flag=True, help="Read the password from stdin (for scripts) instead of prompting"
)
@handle_errors
def workspaces_login_command(email: Optional[str], password_stdin: bool):
    """Sign in with e-mail and password; the session token is kept for the session-only subcommands.

    Accounts created with GitHub or Google sign-in set a password with "Forgot password" on lium.io first.
    Without a terminal (or with LIUM_NONINTERACTIVE=1) nothing is asked: pass --email and --password-stdin.
    """
    lium = workspace_client()
    lium.workspaces.require_enabled()  # nothing to sign in for on a server without workspaces
    email = email or ui.prompt("E-mail", hint="pass --email")
    if password_stdin:
        password = sys.stdin.readline().rstrip("\n")
    elif not is_interactive():
        raise CliFailure(
            "input_required",
            f"Input required: Password (no prompt shown because {noninteractive_reason()}; pass --password-stdin)",
            EXIT_CONFIGURATION_ERROR,
        )
    else:
        password = click.prompt("Password", hide_input=True)
    token = lium.workspaces.login(email, password)
    settings.set("session.token", token)
    ui.success("Signed in; `lium workspaces` subcommands can now manage your teams")


@workspaces_command.command("create")
@click.argument("name")
@click.option("--use", "make_default", is_flag=True, help="Also make it the default workspace")
@handle_errors
def workspaces_create_command(name: str, make_default: bool):
    """Create a workspace; you become its owner and billing owner."""
    lium = workspace_client()
    require_session(lium)
    lium.workspaces.require_enabled()
    if make_default:
        # a name config.ini cannot hold, or a section another workspace already holds, is refused before the
        # workspace exists: a refusal after the POST would leave a workspace a retry on exit 2 duplicates
        _free_section(name, None)
    workspace = lium.workspaces.create(name)
    ui.success(f"Created {escape(workspace.name)} ({workspace.id})")
    if make_default:
        _remember(workspace)
    ui.dim(f"Next: lium keys create <name> --workspace {escape(workspace.name)} --save")


@workspaces_command.command("invite")
@click.argument("email")
@WORKSPACE_ARG
@click.option("--role", type=ROLES, default="member", show_default=True)
@handle_errors
def workspaces_invite_command(email: str, workspace: Optional[str], role: str):
    """E-mail an invitation to join a workspace; the address need not have a Lium account yet."""
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    invitation = lium.workspaces.invite(target.id, email, role)
    if invitation.get("email_sent") is False:
        ui.warning(f"Invitation recorded but the e-mail to {escape(email)} did not leave; revoke it on lium.io and retry")
    else:
        ui.success(
            f"Invited {escape(email)} to {escape(target.name)} as {role}; "
            f"the link expires {escape(str(invitation.get('expires_at', '')))}"
        )


@workspaces_command.command("remove")
@click.argument("who", metavar="USER_ID_OR_EMAIL")
@WORKSPACE_ARG
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@handle_errors
def workspaces_remove_command(who: str, workspace: Optional[str], yes: bool):
    """Remove a member from a workspace (an owner or the billing owner cannot be removed — the server says so)."""
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    user_id = _member_id(lium, target, who)
    if not yes and not ui.confirm(f"Remove {escape(who)} from {escape(target.name)}?"):
        ui.warning("Nothing removed")
        return
    ui.success(escape(lium.workspaces.remove_member(target.id, user_id).get("message", "Member removed")))


@workspaces_command.command("transfer-billing")
@click.argument("who", metavar="USER_ID_OR_EMAIL")
@WORKSPACE_ARG
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@handle_errors
def workspaces_transfer_billing_command(who: str, workspace: Optional[str], yes: bool):
    """Hand the bill to another member: at once for an owner or admin, after their acceptance for a member."""
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    user_id = _member_id(lium, target, who)
    if not yes and not ui.confirm(f"Hand the bill for {escape(target.name)} to {escape(who)}? Their balance pays from then on"):
        ui.warning("Nothing transferred")
        return
    after = lium.workspaces.transfer_billing(target.id, user_id)
    if after.pending_billing_owner_user_id:
        ui.success(f"Transfer requested; {escape(who)} pays for {escape(target.name)} once they accept on lium.io")
    else:
        ui.success(f"{escape(who)} now pays for {escape(target.name)}")


@workspaces_command.command("delete")
@WORKSPACE_ARG
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@handle_errors
def workspaces_delete_command(workspace: Optional[str], yes: bool):
    """Delete a workspace (owners only; refused while it still has running pods or volumes).

    Its `[workspace.<name>]` section (id and saved key, under whatever name it was saved) is dropped from
    ~/.lium/config.ini, and so is `[workspaces] active` when it named this workspace.
    """
    lium = workspace_client()
    require_session(lium)
    target = target_workspace(lium, workspace)
    if not yes and not ui.confirm(f"Delete workspace {escape(target.name)} ({target.id})?"):
        ui.warning("Nothing deleted")
        return
    ui.success(escape(lium.workspaces.delete(target.id).get("message", "Workspace deleted")))
    _forget(target)


__all__ = ["workspaces_command", "workspace_client", "target_workspace", "require_session", "section_for"]
