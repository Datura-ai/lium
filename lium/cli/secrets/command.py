"""`lium secrets`: named values delivered to pods as files, never as env vars or argv (DAH-1482).

Experimental: hidden, and refused, unless LIUM_SECRETS_ENABLED=1.
"""

import json
import sys

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, ensure_config, format_date, handle_errors
from lium.sdk import Lium
from lium.sdk.secrets import SECRETS_DISABLED, secrets_enabled, validate_secret_name

VALUE_NOT_ON_ARGV = (
    "A secret value is never taken from the command line (it would stay in shell history and in `ps`): "
    "pipe it on stdin (`lium secrets set NAME < file`) or type it at the hidden prompt"
)


def require_enabled() -> None:
    if not secrets_enabled():
        raise CliFailure("secrets_disabled", SECRETS_DISABLED, EXIT_CONFIGURATION_ERROR)


def checked_name(name: str) -> str:
    try:
        return validate_secret_name(name)
    except ValueError as e:
        raise CliFailure("invalid_arguments", str(e), EXIT_CONFIGURATION_ERROR)


def stdin_is_interactive() -> bool:
    return sys.stdin.isatty()


def read_secret_value(name: str) -> str:
    """From stdin when it is piped (one trailing newline dropped), else a hidden prompt typed twice."""
    if stdin_is_interactive():
        value = click.prompt(f"Value for {name}", hide_input=True, confirmation_prompt=True, default="", show_default=False)
    else:
        value = sys.stdin.read()
        if value.endswith("\r\n"):
            value = value[:-2]
        elif value.endswith("\n"):
            value = value[:-1]
    if not value:
        raise CliFailure("invalid_arguments", f"Secret {name} needs a non-empty value", EXIT_CONFIGURATION_ERROR)
    return value


@click.group("secrets", invoke_without_command=True, hidden=not secrets_enabled())
@click.pass_context
def secrets_command(ctx):
    """Secrets for pods, delivered as files under /run/lium/secrets/ (experimental)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(secrets_list_command)


@secrets_command.command("list")
@click.option("--json", "json_output", is_flag=True, help="Machine-readable output (names and times only)")
@handle_errors
def secrets_list_command(json_output: bool):
    """List secret names and when each last changed; values are never shown."""
    require_enabled()
    ensure_config()
    secrets = Lium().secrets.list()
    if json_output:
        click.echo(json.dumps([{"name": s.name, "updated_at": s.updated_at} for s in secrets], indent=2))
        return
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))
    for column in ("Name", "Updated"):
        table.add_column(column, overflow="fold")
    for secret in secrets:
        table.add_row(Text(secret.name), format_date(secret.updated_at) if secret.updated_at else "—")
    ui.info(f"Secrets  ({len(secrets)} total)")
    ui.print(table)


@secrets_command.command(
    "set", context_settings={"ignore_unknown_options": True, "allow_extra_args": True}
)
@click.argument("name")
@click.pass_context
@handle_errors
def secrets_set_command(ctx, name: str):
    """\b
    Create or replace secret NAME. The value comes from stdin or a hidden prompt, never argv.
    \b
    Examples:
      lium secrets set HF_TOKEN                   # hidden prompt, typed twice
      lium secrets set HF_TOKEN < ~/.hf_token     # from a file
      pass show hf | lium secrets set HF_TOKEN    # from a password manager
    """
    require_enabled()
    if ctx.args:
        # click would echo an unexpected argument back; this one may be the value itself
        raise CliFailure("invalid_arguments", VALUE_NOT_ON_ARGV, EXIT_CONFIGURATION_ERROR)
    checked_name(name)
    ensure_config()
    value = read_secret_value(name)
    Lium().secrets.set(name, value)
    ui.success(f"Secret {escape(name)} saved")


@secrets_command.command("rm")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@handle_errors
def secrets_rm_command(name: str, yes: bool):
    """Delete secret NAME. Pods already running keep the copy they were given."""
    require_enabled()
    checked_name(name)
    ensure_config()
    if not yes and not ui.confirm(f"Delete secret {escape(name)}?"):
        return
    Lium().secrets.delete(name)
    ui.success(f"Secret {escape(name)} deleted")
