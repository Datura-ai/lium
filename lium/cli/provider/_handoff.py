"""The human-handoff contract: a step only a person can do, as one URL plus a short code.

``run_handoff`` asks the portal for a handoff session (``POST /auth/handoffs``). Without ``wait`` it raises
``human.handoff_required`` (exit 12) with ``data: {step, handoff_id, handoff_url, code, expires_at,
message_for_human}``: the agent relays ``message_for_human`` and runs the command again, or with ``--wait``.
With ``wait`` it first prints the handoff (one ``{"event": "handoff", …}`` line on stderr under ``--json``,
the sentence itself otherwise), then polls ``GET /auth/handoffs/{id}`` until the person is done (returns),
the code expires (``human.handoff_expired``, exit 12) or ``timeout`` seconds pass (``human.handoff_required``
again, with ``waited_s``). A poll the portal could not answer (a 5xx, a 429, nothing reachable) is retried with
backoff within the timeout; a handoff the portal no longer knows (404 ``handoff_not_found``) has expired.

A portal without handoff sessions answers ``portal.not_supported`` (exit 3); the caller's ``legacy_url``
(the old browser flow) goes into ``data.legacy_browser_url`` with ``data.legacy_flow: true``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

import click

from lium.provider.errors import (
    HANDOFF_EXPIRED,
    HANDOFF_REQUIRED,
    NET_UNREACHABLE,
    PORTAL_NOT_SUPPORTED,
    PORTAL_RATE_LIMIT,
    PORTAL_SERVER_ERROR,
    ProviderError,
    ProviderServerError,
)

HANDOFF_FIELDS = ("step", "handoff_id", "handoff_url", "code", "expires_at", "message_for_human")
DONE_STATUSES = ("completed",)
EXPIRED_STATUSES = ("expired",)
CLAIM_GRACE_S = 15 * 60
"""After the code is entered the portal keeps the handoff this long past ``expires_at`` (the OAuth consent, the inbox)."""
MAX_BACKOFF_S = 30.0


def run_handoff(
    client: Any,
    *,
    step: str,
    wait: bool,
    timeout: float | None,
    poll_interval: float,
    json_mode: bool,
    legacy_url: Callable[[], str | None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Run ``step`` through a handoff; returns ``{step, done: True, …}`` once the person has finished."""
    try:
        answer = client.create_handoff(step)
    except ProviderError as e:
        if e.code == PORTAL_NOT_SUPPORTED:
            raise _not_supported(step, e, legacy_url) from e
        if e.code == "portal.handoff_step_done":
            return {"step": step, "done": True, "already_done": True}
        raise
    data = {k: answer.get(k) for k in HANDOFF_FIELDS}
    data["step"] = answer.get("step") or step
    missing = [k for k in ("handoff_id", "handoff_url", "code") if not data[k]]
    if missing:
        raise ProviderError(
            f"the portal's handoff answer has no {', '.join(missing)}",
            code="PORTAL_CONTRACT_DRIFT",
            context={"step": step, "missing": missing},
        )
    if not data["message_for_human"]:
        data["message_for_human"] = (
            f"Open {data['handoff_url']} and enter code {data['code']} (expires {data['expires_at']})."
        )
    if not wait:
        raise ProviderError(data["message_for_human"], code=HANDOFF_REQUIRED, context=data)

    if json_mode:
        click.echo(json.dumps({"event": "handoff", **data}, sort_keys=True), err=True)
    else:
        click.echo(data["message_for_human"], err=True)
    started = clock()
    deadline = started + (timeout if timeout is not None else _seconds_until_gone(data["expires_at"]))
    failures = 0
    while True:
        try:
            answer = client.get_handoff(data["handoff_id"]) or {}
        except ProviderError as e:
            if e.code == "portal.handoff_not_found":
                raise ProviderError(
                    f"the {data['step']} handoff is gone (expired or replaced by a newer one)",
                    code=HANDOFF_EXPIRED,
                    cause=e,
                    context={**data, "status": "not_found"},
                ) from e
            if not _transient(e):
                raise
            failures += 1
            pause = min(poll_interval * 2 ** (failures - 1), MAX_BACKOFF_S)
            if clock() + pause > deadline:
                raise
            sleep(pause)
            continue
        failures = 0
        status = str(answer.get("status") or "")
        if status in DONE_STATUSES:
            return {"step": data["step"], "done": True, "handoff_id": data["handoff_id"]}
        if status in EXPIRED_STATUSES:
            raise ProviderError(
                f"the {data['step']} code expired before the person finished",
                code=HANDOFF_EXPIRED,
                context={**data, "status": status},
            )
        waited = clock() - started
        if timeout is not None and waited >= timeout:
            raise ProviderError(
                data["message_for_human"],
                code=HANDOFF_REQUIRED,
                context={**data, "status": status, "waited_s": round(waited, 1)},
            )
        sleep(poll_interval)


def _transient(e: ProviderError) -> bool:
    """A poll worth retrying: the portal failed or was unreachable, not a refusal."""
    return (
        isinstance(e, ProviderServerError)
        or e.code in (NET_UNREACHABLE, PORTAL_SERVER_ERROR, PORTAL_RATE_LIMIT)
        or e.context.get("status") in (429, 502, 503, 504)
    )


def _seconds_until_gone(expires_at: Any) -> float:
    """Without --timeout, poll errors are retried until the portal would have dropped the handoff anyway."""
    try:
        ends = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        left = (ends - datetime.now(timezone.utc)).total_seconds()
    except ValueError:
        left = 10 * 60
    return max(left, 60.0) + CLAIM_GRACE_S


def _not_supported(step: str, cause: ProviderError, legacy_url: Callable[[], str | None] | None) -> ProviderError:
    context: dict[str, Any] = {"step": step, "status": cause.context.get("status"), "legacy_flow": True}
    url = None
    if legacy_url is not None:
        try:
            url = legacy_url()
        except ProviderError as e:
            context["legacy_browser_url_error"] = e.code
    context["legacy_browser_url"] = url
    return ProviderError(
        "this portal does not serve human handoff sessions yet; data.legacy_browser_url is the old browser flow "
        "(a person opens it in a browser signed in to the account)",
        code=PORTAL_NOT_SUPPORTED,
        hint="Give data.legacy_browser_url to the person, or retry once the portal serves /auth/handoffs.",
        context=context,
    )


__all__ = ["run_handoff"]
