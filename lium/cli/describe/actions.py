import contextlib
import re

import requests

from lium.sdk import Lium, LiumError, PodInfo
from lium.cli.utils import loading_status

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)


def resolve_pod(lium: Lium, target: str, show_progress: bool) -> PodInfo | None:
    """The pod matching TARGET by id, huid or name, or None when nothing listed matches.

    Errors raised by the SDK travel up untouched — classifying them here by
    message would misreport an API outage as a mistyped pod id.
    """
    with loading_status("Loading pod", "") if show_progress else contextlib.nullcontext():
        pods = lium.ps()

    return next((p for p in pods if target in (p.id, p.huid, p.name)), None)


def pod_history(lium: Lium, target: str) -> list[dict]:
    """The event log of a pod that is no longer listed, when TARGET is its id.

    A deleted pod has no row for `ps` to return, but the backend keeps its events (DAH-2927):
    the delete, the reason, a failed reboot's cause. Only a pod id can be looked up — a huid
    is derived from the id and a name is not unique — so anything else yields [].
    """
    if not _UUID.match(target):
        return []
    try:
        return lium.pod_events(target)
    except LiumError:
        return []


def pod_detail(lium: Lium, pod_id: str) -> dict:
    """GET /pods/{id}: the fields `ps` does not carry (last_event, the node's disk verdict).

    Best effort — a detail call failing must not take `describe` down with it, the listing
    already answered. `Lium.pod()` re-raises a transport error (`requests.RequestException`)
    after its retries as-is, not as a `LiumError`, so both are swallowed here.
    """
    try:
        detail = lium.pod(pod_id)
    except (LiumError, requests.RequestException):
        return {}
    return detail if isinstance(detail, dict) else {}
