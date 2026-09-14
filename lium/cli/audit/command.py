"""Audit command: who did what to the account's pods, and when."""

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click

from lium.sdk import Lium
from lium.sdk.exceptions import LiumAuthError, LiumNotFoundError, LiumPermissionError
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_POD_NOT_FOUND,
    _api_error_data,
    ensure_config,
    handle_errors,
    parse_targets,
)
from lium.cli.rm.parsing import parse_duration

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# What a renter reads for the backend's sub_event_type; anything unlisted is shown as recorded.
_WHAT = {
    "pod-rent.requested": "rent requested",
    "pod-create.success": "created",
    "pod-create.failed": "create failed",
    "pod-delete.success": "delete requested",
    "pod-delete.failed": "delete failed",
    "worker-pod-delete.success": "deleted by the platform",
    "pod-reboot.success": "reboot requested",
    "pod-reboot.failed": "reboot failed",
    "pod-edit.success": "edited",
    "pod-switch-template.success": "template switched",
    "pod-add-ssh-key.success": "ssh key added",
    "pod-host-reboot-recovery.recovered": "recovered after host reboot",
    "api-key-create.success": "API key created",
    "api-key-delete.success": "API key deleted",
    "api-key-update.success": "API key renamed",
    "ssh-key-create.success": "SSH key added",
    "ssh-key-delete.success": "SSH key removed",
    "template-create.success": "template created",
    "template-delete.success": "template deleted",
}


def parse_since(value: str) -> datetime:
    """``24h`` / ``30m`` / ``7d`` counted back from now, or an ISO-8601 timestamp; UTC either way."""
    delta, _ = parse_duration(value)
    if delta is not None:
        return datetime.now(timezone.utc) - delta
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise CliFailure(
            "invalid_since",
            f"Invalid --since '{value}'. Use a duration like 24h, 30m, 7d or an ISO timestamp.",
            EXIT_CONFIGURATION_ERROR,
        )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def what(event: Dict[str, Any]) -> str:
    """One phrase for the event: lifecycle entries say the status change and its reason."""
    sub = event.get("sub_event_type") or event.get("event_type") or "?"
    if sub == "pod-lifecycle.status":
        text = f"→ {event.get('to_status')}"
        if event.get("reason"):
            text += f" ({event['reason']})"
    else:
        text = _WHAT.get(sub, sub)
    for extra in (event.get("detail"), event.get("error")):
        if extra:
            text += f": {extra}"
    return text


def who(event: Dict[str, Any]) -> str:
    actor = event.get("actor")
    if not actor:
        return "platform"
    if actor.get("auth") == "api_key":
        return f"key {actor.get('api_key_name') or '?'} ({(actor.get('api_key_id') or '')[:8]})"
    return actor.get("auth") or "?"


def _when(created_at: Optional[str]) -> str:
    if not created_at:
        return "—"
    try:
        stamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return created_at
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc)   # the column is UTC whatever offset the stamp carries
    return stamp.strftime("%Y-%m-%d %H:%M:%SZ")


def rows(events: List[Dict[str, Any]]) -> List[List[str]]:
    # oldest first: the API returns the tail newest-first, a log reads top-down
    return [
        [_when(e.get("created_at")), e.get("pod_name") or (e.get("pod_id") or "")[:8] or "—", what(e), who(e)]
        for e in reversed(events)
    ]


# the account audit log (GET /account/audit, lium-platform DAH-3245): one entry per request that changed something
ACCOUNT_SOURCES = ("portal", "cli", "sdk", "mcp", "admin", "api")
ACCOUNT_LIMIT_MAX = 500


def account_what(entry: Dict[str, Any]) -> str:
    """`pod.cancel_scheduled_removal` reads as `pod: cancel scheduled removal`; a refused request says its status."""
    action = entry.get("action") or "?"
    resource, _, verb = action.partition(".")
    text = f"{resource}: {verb.replace('_', ' ')}" if verb else action
    status = entry.get("status_code")
    if isinstance(status, int) and status >= 400:
        text += f" (refused, {status})" if status < 500 else f" (failed, {status})"
    return text


def account_resource(entry: Dict[str, Any]) -> str:
    result = (entry.get("summary") or {}).get("result") or {}
    name = result.get("name") or result.get("pod_name")
    if name:
        return str(name)
    return (entry.get("resource_id") or "")[:8] or "—"


def account_who(entry: Dict[str, Any]) -> str:
    """Like `who`, but a session actor names its user: in a team account two members are told apart by the
    first 8 characters of `actor.user_id` (the same length the By column gives an API key id)."""
    actor = entry.get("actor")
    if not actor or actor.get("auth") == "api_key":
        return who(entry)
    user_id = str(actor.get("user_id") or "")[:8]
    auth = actor.get("auth") or "?"
    return f"{auth} {user_id}" if user_id else auth


def account_rows(entries: List[Dict[str, Any]]) -> List[List[str]]:
    # oldest first, like the pod log; `ip` is empty on a team-mate's entries — the server shows an address to its
    # owner only
    return [
        [
            _when(e.get("created_at")),
            account_what(e),
            account_resource(e),
            account_who(e),
            e.get("source") or "—",
            e.get("ip") or "—",
        ]
        for e in reversed(entries)
    ]


def _resolve_pod_id(lium: Lium, target: str) -> str:
    # a listed pod by id/huid/name/index; a bare UUID is passed through so a deleted pod can be asked about
    matches = parse_targets(target, lium.ps())
    if len(matches) == 1:
        return matches[0].id
    if _UUID.match(target):
        return target
    raise CliFailure(
        "pod_not_found", f"No listed pod matches '{target}'. For a deleted pod give its full id.", EXIT_POD_NOT_FOUND
    )


@click.command("audit")
@click.option("--pod", "pod", help="Only this pod: id, huid, name or index from the last ps; a deleted pod's full id")
@click.option("--since", "since", help="Only events after this: 24h, 30m, 7d or an ISO timestamp")
@click.option("--key", "api_key_id", help="Only actions made with this API key id")
@click.option("--limit", type=click.IntRange(1, 1000), default=200, show_default=True, help="Newest events to fetch (1–1000)")
@click.option("--json", "json_output", is_flag=True, help="Print the events as machine-readable JSON")
@click.option(
    "--account",
    "account",
    is_flag=True,
    help="The account audit log instead of the pod log: every request that changed something (pods, keys, logins, "
    "balance, settings, team members) with the client and IP it came from; --limit is 1–500 here",
)
@click.option("--action", "action", help="With --account: only this action or prefix (pod., pod.delete, key., login)")
@click.option(
    "--source", "source", type=click.Choice(ACCOUNT_SOURCES), help="With --account: only requests from this client"
)
@click.option(
    "--cursor", "cursor", help="With --account: continue from the next_cursor the previous page printed (older entries)"
)
@handle_errors
def audit_command(
    pod: Optional[str],
    since: Optional[str],
    api_key_id: Optional[str],
    limit: int,
    json_output: bool,
    account: bool,
    action: Optional[str],
    source: Optional[str],
    cursor: Optional[str],
):
    """Show who did what to the account's pods, and when.

    Every rent, reboot, edit and delete names the session or API key that requested it; entries the
    platform wrote by itself (a validator reply, a balance stop) say "platform".

    With --account: the account audit log — one entry per request that changed something (a pod created or
    deleted, a key created or revoked, a login, a top-up requested, a setting or a team member changed),
    with the client (portal, cli, sdk, mcp) and the IP it came from. Your own IPs only; the last 90 days.

    \b
    Examples:
      lium audit                       # last 200 events, oldest first
      lium audit --since 24h           # what happened today
      lium audit --pod my-pod          # one pod's history, also after it was deleted (full id)
      lium audit --key 3f2a...         # everything one API key did (the full id; --json shows it as actor.api_key_id)
      lium audit --json | jq '.[] | select(.actor.api_key_name == "ci")'
      lium audit --account --since 7d  # who did what from where, this week
      lium audit --account --action pod.delete --source cli --json
      lium audit --account --cursor <next_cursor>   # the next (older) page, as the previous page printed it
    """
    if not json_output:
        ensure_config()

    if (action or source or cursor) and not account:
        raise CliFailure(
            "account_only",
            "--action, --source and --cursor need --account.",
            EXIT_CONFIGURATION_ERROR,
        )
    if account and limit > ACCOUNT_LIMIT_MAX:
        raise CliFailure(
            "limit",
            f"--account lists at most {ACCOUNT_LIMIT_MAX} entries per call (--limit {limit}).",
            EXIT_CONFIGURATION_ERROR,
        )

    if api_key_id and not _UUID.match(api_key_id):
        # the server declares api_key_id as a UUID; the 8 characters the By column prints would come back as a 422
        raise CliFailure(
            "invalid_key",
            f"Invalid --key '{api_key_id}'. --key takes the API key's full id (lium audit --json shows it as actor.api_key_id).",
            EXIT_CONFIGURATION_ERROR,
        )

    lium = Lium()
    since_at = parse_since(since) if since else None
    pod_id = _resolve_pod_id(lium, pod) if pod else None
    if account:
        _account_log(lium, since_at, pod_id, api_key_id, limit, json_output, action, source, cursor)
        return
    try:
        events = lium.events(since=since_at, pod_id=pod_id, api_key_id=api_key_id, limit=limit)
    except LiumAuthError as exc:
        # same exit code as every other command's 401 (handle_errors → EXIT_API_ERROR); only the hint is added,
        # and the server's code, hint and request_id ride along like on a bare LiumError (DAH-3057)
        raise CliFailure(
            exc.code or "auth_error",
            f"{exc}. If the key works for 'lium ps', this backend does not yet open /users/me/events to API keys.",
            EXIT_API_ERROR,
            data=_api_error_data(exc),
            hint=exc.hint,
        )

    if json_output:
        click.echo(json.dumps(events, indent=2, ensure_ascii=False))
        return

    if not events:
        ui.info("No events" + (f" since {since}" if since else "") + ".")
        return
    ui.table(["When (UTC)", "Pod", "What", "By"], rows(events))


def _account_log(
    lium: Lium,
    since_at: Optional[datetime],
    pod_id: Optional[str],
    api_key_id: Optional[str],
    limit: int,
    json_output: bool,
    action: Optional[str],
    source: Optional[str],
    cursor: Optional[str],
) -> None:
    # --limit 200 is the pod log's default; the account log pages at 100 and caps at 500 — one page is shown per
    # call, the next one with --cursor
    try:
        page = lium.audit_log(
            since=since_at,
            action=action,
            source=source,
            api_key_id=api_key_id,
            resource_id=pod_id,
            cursor=cursor,
            limit=limit,
        )
    except LiumNotFoundError:
        raise CliFailure(
            "not_supported",
            "This backend has no account audit log (GET /account/audit); it ships with lium-platform DAH-3245.",
            EXIT_API_ERROR,
        )
    except LiumPermissionError as exc:
        # the server answers 403 for a key without `read` (and for a team key outside its workspace); the same exit
        # code every other command uses for a 403 (handle_errors → EXIT_PERMISSION_DENIED), only the hint is added,
        # and the server's code, hint and request_id ride along like on the 401 above (DAH-3057)
        raise CliFailure(
            exc.code or "permission_denied",
            f"{exc}. The account log needs the key's `read` scope.",
            EXIT_PERMISSION_DENIED,
            data=_api_error_data(exc),
            hint=exc.hint,
        )

    entries = page.get("items") or []
    if json_output:
        click.echo(json.dumps(page, indent=2, ensure_ascii=False))
        return
    if not entries:
        if cursor:
            ui.info("No older entries.")
            return
        ui.info(
            "No account activity recorded"
            + (" in that window" if since_at else "")
            + "."
        )
        return
    ui.table(
        ["When (UTC)", "What", "Resource", "By", "Client", "IP"], account_rows(entries)
    )
    if page.get("next_cursor"):
        ui.info(
            f"Older entries may exist: lium audit --account --cursor {page['next_cursor']} (with the same filters) "
            "shows the next page."
        )
