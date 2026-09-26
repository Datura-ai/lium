"""The human-handoff contract: a step only a person can do, as one URL plus a short code.

``run_handoff`` asks the portal for a handoff session (``POST /auth/handoffs``). Without ``wait`` it raises
``human.handoff_required`` (exit 12) with ``data: {step, handoff_id, handoff_url, code, expires_at,
message_for_human}``: the agent relays ``message_for_human`` and runs the command again, or with ``--wait``.
With ``wait`` it first prints the handoff (one ``{"event": "handoff", …}`` line on stderr under ``--json``,
the sentence itself otherwise), then polls ``GET /auth/handoffs/{id}`` until the person is done (returns),
the code expires (``human.handoff_expired``, exit 12) or ``timeout`` seconds pass (``human.handoff_required``
again, with ``waited_s``).

A portal without handoff sessions answers ``portal.not_supported`` (exit 3); the caller's ``legacy_url``
(the old browser flow) goes into ``data.legacy_browser_url`` with ``data.legacy_flow: true``.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

import click

from lium.provider.errors import HANDOFF_EXPIRED, HANDOFF_REQUIRED, PORTAL_NOT_SUPPORTED, ProviderError

HANDOFF_FIELDS = ("step", "handoff_id", "handoff_url", "code", "expires_at", "message_for_human")
DONE_STATUSES = ("completed",)
EXPIRED_STATUSES = ("expired",)


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
        session = client.create_handoff(step)
    except ProviderError as e:
        if e.code == PORTAL_NOT_SUPPORTED:
            raise _not_supported(step, e, legacy_url) from e
        if e.code == "portal.handoff_step_done":
            return {"step": step, "done": True, "already_done": True}
        raise
    data = {k: session.get(k) for k in HANDOFF_FIELDS}
    data["step"] = session.get("step") or step
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
    while True:
        status = str((client.get_handoff(data["handoff_id"]) or {}).get("status") or "")
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
