"""`lium whoami`: which key, which account, which machine."""

import json
from typing import Optional

import click
from rich.markup import escape

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, handle_errors, sdk_error_failure
from .identity import Identity, collect_identity


def _yes_no(value):
    if value is None:
        return "unknown"
    return "yes" if value else "no"


def _api_row(identity: Identity) -> str:
    base = identity.api_base_url or ""
    if identity.api_reachable and identity.api_error:
        return f"{base}  answered with an error: {identity.api_error}".strip()
    if identity.api_reachable:
        return f"{base}  reachable, {identity.api_latency_ms} ms"
    return f"{base}  unreachable: {identity.api_error}".strip()


def render(identity: Identity) -> None:
    rows = [
        ("API key", f"{identity.api_key_fingerprint}  ({identity.api_key_source or 'not configured'})"),
        ("Account", identity.account_id or "-"),
    ]
    if identity.email:
        rows.append(("Email", identity.email))
    rows += [
        ("Balance", f"${identity.balance_usd:.2f}" if identity.balance_usd is not None else "-"),
        ("API", _api_row(identity)),
        ("SSH key", (
            f"{identity.ssh_key_path}  registered: {_yes_no(identity.ssh_key_registered)}"
            if identity.ssh_key_path else "none found in ~/.ssh"
        )),
        ("CLI", f"lium {identity.cli_version}"),
    ]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        ui.print(f"[dim]{label.ljust(width)}[/dim]  {escape(value)}")
    for warning in identity.warnings:
        ui.warning(warning)


def failure_for(identity: Identity) -> Optional[CliFailure]:
    """Why `whoami` exits non-zero for this identity, or None. The identity rides along as ``data``."""
    if not identity.has_api_key:
        return CliFailure("no_api_key", "No API key configured", EXIT_CONFIGURATION_ERROR, data=identity.to_dict())
    if identity.api_exception is not None:
        # a 401/403/5xx the API answered with: the same code, exit status and hint as any other command
        return sdk_error_failure(identity.api_exception, data=identity.to_dict())
    if identity.api_reachable is False:
        return CliFailure("api_unreachable", identity.api_error or "API unreachable", EXIT_API_ERROR,
                          data=identity.to_dict())
    return None


@click.command("whoami")
@click.option("--json", "json_output", is_flag=True, help="Print machine-readable JSON")
@handle_errors
def whoami_command(json_output: bool):
    """Show the API key in use (fingerprint and source), the account, balance, SSH key and CLI version.

    \b
    Examples:
      lium whoami
      lium whoami --json | jq .account_id
    """
    identity = ui.load("Checking", collect_identity) if not json_output else collect_identity()
    failure = failure_for(identity)

    if json_output:
        # stdout carries either the identity or nothing: on failure the envelope on stderr has it as `data`
        if failure:
            raise failure
        click.echo(json.dumps(identity.to_dict(), sort_keys=True))
        return

    render(identity)
    if failure:
        raise failure
