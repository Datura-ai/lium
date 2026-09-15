"""Pods (ps) command implementation."""

import json
import time
from typing import List, Optional, Tuple

import click

from lium.sdk import Lium, PodInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_POD_NOT_FOUND,
    console,
    handle_errors,
    ensure_config,
    store_pod_selection,
    resolve_output_format,
)
from lium.cli.workspaces.context import show_workspace
from . import display, selection
from .actions import GetPodsAction

# Below this many columns the Ports column makes every row wrap; drop it unless --wide.
WIDE_TERMINAL_COLUMNS = 120


def _terminal_width() -> int:
    """Columns available, or "wide" when stdout is not a terminal: Rich reports 80
    for a pipe, and cron/CI output must keep every column (Ports carries the IP)."""
    try:
        if not console.is_terminal:
            return WIDE_TERMINAL_COLUMNS
        return int(console.size.width) or WIDE_TERMINAL_COLUMNS
    except Exception:  # noqa: BLE001 - no terminal at all
        return WIDE_TERMINAL_COLUMNS


def _load_pods(lium: Lium, quiet: bool) -> List[PodInfo]:
    action = GetPodsAction()
    ctx = {"lium": lium}
    if quiet:
        return action.execute(ctx).data["pods"]
    return ui.load("Loading pods", lambda: action.execute(ctx)).data["pods"]


def _select(
    pods: List[PodInfo], pod_id: Optional[str], filters: List[Tuple[str, str]], sort_key: Optional[str], reverse: bool
) -> List[PodInfo]:
    if pod_id:
        pod = next((p for p in pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)
        if not pod:
            raise CliFailure(
                "pod_not_found",
                f"Pod '{pod_id}' not found. If it was deleted, 'lium describe <pod id>' shows its last events.",
                EXIT_POD_NOT_FOUND,
            )
        pods = [pod]
    pods = [p for p in pods if selection.matches(p, filters)]
    return selection.sort_pods(pods, sort_key, reverse)


def _last_event(lium: Lium, pods: List[PodInfo], pod_id: Optional[str]) -> Optional[dict]:
    """`ps <pod>` only: GET /pods/{id} for why the pod is REBOOT_FAILED / BROKEN (DAH-2932).

    Imported here, not at the top: describe.display imports ps.display, and a module-level import
    back into describe from this package's __init__ chain made `import lium.cli.describe.display`
    fail with a partially initialised module.
    """
    if not pod_id or not pods:
        return None
    from lium.cli.describe.actions import pod_detail
    from lium.cli.describe.display import event_view

    return event_view(pod_detail(lium, pods[0].id).get("last_event"))


def _render(
    pods: List[PodInfo], output_format: str, wide: bool, filtered: bool, show_index: bool,
    last_event: Optional[dict] = None, account: Optional[str] = None, lium: Optional[Lium] = None,
) -> None:
    if output_format == "json":
        # No account line here: the machine contract is a bare JSON array on stdout and, on
        # failure, one JSON object on stderr — a script reading stderr must not find prose.
        payload = [
            display.compact_pod(p, index=position if show_index else None)
            for position, p in enumerate(pods, start=1)
        ]
        if not show_index and payload:
            payload[0]["last_event"] = last_event
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if not pods:
        ui.warning("No pods match the filters" if filtered else "No active pods")
        if account:
            ui.dim(account)
        if lium is not None:
            show_workspace(lium)
        return
    short = not wide and _terminal_width() < WIDE_TERMINAL_COLUMNS
    table, header = display.build_pods_table(pods, short=short, show_index=show_index)
    ui.info(header)
    ui.print(table)
    if short:
        ui.dim("Ports hidden on a narrow terminal; use --wide or --format json")
    if last_event:
        from lium.cli.describe.display import format_event

        ui.dim(f"last event: {format_event(last_event)}")
    if account:
        ui.dim(account)
    # The workspace line under the table (lium#183), as on main; never in JSON output.
    if lium is not None:
        show_workspace(lium)


@click.command("ps", epilog="Use --format json for machine-readable output.")
@click.argument("pod_id", required=False)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@click.option(
    "--sort", "sort_key", type=click.Choice(selection.SORT_KEYS),
    help="Sort rows. Row numbers follow the order shown: 'lium rm 1' is the pod on row 1 of this listing",
)
@click.option("--reverse", "-r", is_flag=True, help="Reverse the sort order")
@click.option(
    "--filter", "filters", multiple=True, metavar="KEY=VALUE",
    help=f"Keep pods whose field starts with VALUE (case-insensitive); repeatable. KEY: {', '.join(selection.FILTER_KEYS)}",
)
@click.option("--watch", "-w", type=float, metavar="SECONDS", help="Refresh every N seconds until interrupted")
@click.option("--wide", is_flag=True, help="Always show every column, including ports")
@handle_errors
def ps_command(
    pod_id: Optional[str],
    output_format: str,
    json_output: bool,
    sort_key: Optional[str],
    reverse: bool,
    filters: Tuple[str, ...],
    watch: Optional[float],
    wide: bool,
):
    """List active GPU pods.

    \b
    The # column (and "index" in --format json) is the row number that rm, ssh,
    exec and scp accept in place of a pod huid. It stands for the pod shown on
    that row of this listing, sorted or filtered as shown. It is honoured only in
    this shell, for 10 minutes and while that pod is still listed; the huid is
    the stable identifier for scripts.

    \b
    Examples:
      lium ps                                  # table; ports hidden on narrow terminals
      lium ps --wide                           # every column
      lium ps --format json | jq '.[].huid'
      lium ps my-pod --format json             # one pod
      lium ps --filter status=RUNNING --sort spent
      lium ps --filter gpu=H100 --filter name=train
      lium ps --watch 10                       # refresh every 10 s
    """
    output_format = resolve_output_format(output_format, json_output)
    if watch is not None and watch <= 0:
        raise CliFailure("invalid_arguments", "--watch must be a positive number of seconds", EXIT_CONFIGURATION_ERROR)
    try:
        parsed_filters = selection.parse_filters(filters)
    except ValueError as exc:
        raise CliFailure("invalid_arguments", str(exc), EXIT_CONFIGURATION_ERROR)

    ensure_config()
    lium = Lium()

    # The table always says which account answered. Two shells can hold different keys (a stale
    # LIUM_API_KEY vs ~/.lium/config.ini), and a pod list without its account is ambiguous —
    # an empty one even reads as an outage. JSON output carries no such line (see _render).
    key_config = getattr(lium, "config", None)
    account = f"Account: {key_config.api_key_description}" if key_config is not None else None

    def once(quiet: bool) -> None:
        pods = _select(_load_pods(lium, quiet), pod_id, parsed_filters, sort_key, reverse)
        if not pod_id:
            # The listing shown defines what "pod 1" means (DAH-2559): the rows in
            # the order shown, sorted or filtered as shown, and the last refresh of
            # --watch. `ps <pod>` shows no row number and leaves the snapshot alone.
            store_pod_selection(pods)
        _render(
            pods, output_format, wide, filtered=bool(parsed_filters), show_index=not pod_id,
            last_event=_last_event(lium, pods, pod_id), account=account, lium=lium,
        )

    if watch is None:
        once(quiet=output_format == "json")
        return
    # Ctrl-C exits 0 wherever the loop is — during the first fetch too, not only after it.
    try:
        once(quiet=output_format == "json")
        while True:
            time.sleep(watch)
            if output_format != "json":
                click.clear()
            once(quiet=True)
    except KeyboardInterrupt:
        return
