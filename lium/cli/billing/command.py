"""`lium billing`: what the ledger charged (`GET /billing/statement`), per pod and, with `--key`, per API key."""

import json
import re
from typing import Any, Dict, List, Optional

import click
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.keys.resolve import UNSTAMPED, key_id_for, rented_through, say_unfiltered
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR, ensure_config, handle_errors, resolve_output_format
from lium.cli.workspaces.context import show_workspace
from lium.sdk import Lium

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _day(option: str, value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not _DAY_RE.match(value):
        raise CliFailure("invalid_arguments", f"{option} must be a UTC day as YYYY-MM-DD, not '{value}'", EXIT_CONFIGURATION_ERROR)
    return value


def _usd(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


def _hours(seconds: Any) -> str:
    try:
        return f"{float(seconds) / 3600:.1f} h"
    except (TypeError, ValueError):
        return "—"


def build_table(pods: List[Dict[str, Any]], show_key: bool) -> Table:
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, padding=(0, 1))
    table.add_column("Pod", overflow="fold")
    table.add_column("GPU", no_wrap=True)
    if show_key:
        table.add_column("Key", overflow="fold")
    table.add_column("Billed", justify="right", no_wrap=True)
    table.add_column("Total", justify="right", no_wrap=True)
    table.add_column("From (UTC)", no_wrap=True)
    table.add_column("To (UTC)", no_wrap=True)
    for pod in pods:
        gpu = Text(f"{pod.get('gpu_count') or ''}×{pod.get('gpu_name')}" if pod.get("gpu_name") else "—")
        row = [Text(str(pod.get("pod_name") or pod.get("pod_id") or "—")), gpu]
        if show_key:
            row.append(Text(str(pod.get("api_key_name") or pod.get("api_key_id") or "—")))
        row += [
            _hours(pod.get("billed_seconds")),
            _usd(pod.get("total")),
            str(pod.get("created_at") or "—").replace("T", " ")[:16],
            (str(pod.get("removed_at")).replace("T", " ")[:16] if pod.get("removed_at") else "running"),
        ]
        table.add_row(*row)
    return table


@click.group("billing")
def billing_command():
    """The account's charges as the ledger recorded them.

    \b
    Examples:
      lium billing history                       # every pod charged, removed ones included
      lium billing history --key agent-1         # what one API key's pods cost
    """


@billing_command.command("history", epilog="Use --format json for machine-readable output.")
@click.option("--from", "start_day", metavar="YYYY-MM-DD", help="First UTC billing day to include (inclusive)")
@click.option("--to", "end_day", metavar="YYYY-MM-DD", help="Last UTC billing day to include (inclusive)")
@click.option(
    "--key", "api_key", metavar="NAME|ID",
    help="Only the pods rented through this API key (GET /billing/statement?api_key_id=…; needs a server that stamps "
         "charges with their key — not on lium.io yet; an older server answers for every pod and the CLI says so). "
         "A name needs `lium workspaces login`; an id does not",
)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def billing_history_command(
    start_day: Optional[str], end_day: Optional[str], api_key: Optional[str], output_format: str, json_output: bool
):
    """What each pod was charged, from the ledger (`GET /billing/statement`), most recently billed first.

    Every pod the account paid for in the period, removed pods included; a pod's Total is the sum of its
    per-day ledger rows — the figure the balance was debited, unlike `lium spend`'s estimate. The key needs
    the `read` scope. `--format json` prints the server's statement: `total` and `pods`, each with its `days`.

    \b
    Examples:
      lium billing history
      lium billing history --from 2026-09-01 --to 2026-09-21
      lium billing history --key agent-1 --format json | jq '.total'
    """
    output_format = resolve_output_format(output_format, json_output)
    start_day, end_day = _day("--from", start_day), _day("--to", end_day)
    if start_day and end_day and end_day < start_day:
        raise CliFailure("invalid_arguments", f"--to ({end_day}) is before --from ({start_day})", EXIT_CONFIGURATION_ERROR)
    ensure_config()
    lium = Lium()
    api_key_id = key_id_for(lium, api_key) if api_key else None

    def fetch() -> Dict[str, Any]:
        return lium.billing_statement(start_day=start_day, end_day=end_day, api_key_id=api_key_id)

    statement = fetch() if output_format == "json" else ui.load("Loading the statement", fetch)
    scope = ""  # already escaped: the key is the user's text
    if api_key_id:
        # a server before per-key charges ignores `api_key_id` and stamps no pod: the statement is then the
        # whole account's — say so, and never head it "through key …"; a server that stamps them filtered (or
        # is filtered here) and the total is the kept pods'
        kept, could_filter = rented_through(statement.get("pods") or [], api_key_id, lambda p: p.get("api_key_id", UNSTAMPED))
        if could_filter:
            scope = f" through key {escape(api_key)}"
            if len(kept) != len(statement.get("pods") or []):
                statement = {**statement, "pods": kept, "total": sum(float(p.get("total") or 0) for p in kept)}
        else:
            say_unfiltered("charge", output_format == "json")
    if output_format == "json":
        click.echo(json.dumps(statement, indent=2, ensure_ascii=False))
        return

    pods = statement.get("pods") or []
    # the server's day strings, printed literally
    period = escape(" to ".join(str(p) for p in (statement.get("start_day"), statement.get("end_day")) if p) or "all time")
    if not pods:
        ui.warning(f"No charges{scope} ({period})")
        show_workspace(lium)
        return
    ui.info(f"Charges{scope}: {len(pods)} pod{'s' if len(pods) != 1 else ''}, {period}")
    ui.print(build_table(pods, show_key=any(p.get("api_key_name") or p.get("api_key_id") for p in pods)))
    ui.dim(escape(f"Total {_usd(statement.get('total'))} — what the ledger debited; running pods keep accruing"))
    show_workspace(lium)
