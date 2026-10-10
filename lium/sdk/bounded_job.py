"""One bounded GPU job: rent by spec, run a command, copy the outputs back, always remove the pod.

The caller states the job (a command, files in, files out), the hardware (a GPU type and count,
plus any :meth:`Lium.rent` constraint), a budget in USD and a deadline in seconds. The pod's
removal is scheduled server-side right after the rent for the earlier of the two limits, so the
pod goes away even when this process dies; the ``finally`` block removes it at once otherwise.
The result names a terminal status, the exit code, the outputs copied, how the pod was cleaned
up and what it cost.
"""

from __future__ import annotations

import math
import shlex
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .exceptions import PodStartError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .client import Lium

#: Terminal statuses of :class:`JobResult`.
SUCCEEDED = "succeeded"            # the command exited 0
FAILED = "failed"                  # the command exited non-zero
TIMED_OUT = "timed_out"            # the deadline stopped the command, or left no time to start it
BUDGET_EXHAUSTED = "budget_exhausted"  # as timed_out, but the budget was the earlier limit
RENT_FAILED = "rent_failed"        # nothing matched the spec, the balance or a key budget refused, …
BOOT_FAILED = "boot_failed"        # the pod was rented but never became ready
ERROR = "error"                    # anything else after the rent (SSH, an input upload, …)

# `timeout` exits 124 when its TERM stopped the command, 137 when the KILL after `-k` had to
_TIMEOUT_EXITS = (124, 137)
_DELETE_ATTEMPTS = 3


@dataclass
class JobResult:
    """What :meth:`Lium.run_job` did. ``to_dict()`` is JSON-ready.

    ``cleanup`` is ``"removed"`` (the pod was deleted before returning), ``"scheduled"`` (the
    delete failed; the server removes the pod at ``removal_scheduled_at``), ``"not_rented"``, or
    ``"unknown"`` (the rent call failed and the pod list could not be read to check for a pod it
    may have created). ``cost_usd`` is what the billing statement shows for the pod once it was
    removed, ``None`` when the statement could not be read or does not list it yet;
    ``estimated_cost_usd`` is the rent's hourly price times the seconds from rent to removal.
    """

    status: str
    name: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None
    pod_id: Optional[str] = None
    price_per_hour: Optional[float] = None
    removal_scheduled_at: Optional[str] = None
    outputs: Dict[str, str] = field(default_factory=dict)
    missing_outputs: Dict[str, str] = field(default_factory=dict)
    cleanup: str = "not_rented"
    cost_usd: Optional[float] = None
    estimated_cost_usd: Optional[float] = None
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == SUCCEEDED

    def to_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


def run_job(
    lium: "Lium",
    *,
    command: str,
    gpu_type: str,
    max_cost_usd: float,
    deadline_s: float,
    gpu_count: int = 1,
    inputs: Optional[Dict[str, str]] = None,
    outputs: Optional[Dict[str, str]] = None,
    env: Optional[Dict[str, str]] = None,
    name: Optional[str] = None,
    boot_timeout: float = 600,
    output_reserve_s: float = 120,
    **rent_kwargs: Any,
) -> JobResult:
    """See :meth:`Lium.run_job`."""
    if max_cost_usd <= 0:
        raise ValueError(f"max_cost_usd must be positive, got {max_cost_usd!r}")
    if deadline_s <= 0:
        raise ValueError(f"deadline_s must be positive, got {deadline_s!r}")
    refused = {"name", "dry_run"} & set(rent_kwargs)
    if refused:
        raise TypeError(f"run_job() does not take {sorted(refused)}")

    started = time.monotonic()
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=deadline_s)
    result = JobResult(status=ERROR, name=name or f"job-{uuid.uuid4().hex[:8]}")
    rented_at: Optional[datetime] = None
    stop_at = deadline_at
    try:
        try:
            rented = lium.rent(gpu_type=gpu_type, gpu_count=gpu_count, name=result.name, **rent_kwargs)
        except Exception as exc:  # noqa: BLE001 - every rent failure is this job's terminal state
            result.status, result.error = RENT_FAILED, str(exc)
            # the server may have created the pod before the response was lost; the name is this call's own
            result.cleanup = _remove_by_name(lium, result.name)
            return result

        rented_at = datetime.now(timezone.utc)
        result.pod_id = rented.pod["id"]
        result.price_per_hour = rented.price_per_hour
        if rented.price_per_hour and rented.price_per_hour > 0:
            budget_at = rented_at + timedelta(hours=max_cost_usd / rented.price_per_hour)
            stop_at = min(deadline_at, budget_at)
        stopped_by = BUDGET_EXHAUSTED if stop_at < deadline_at else TIMED_OUT
        result.removal_scheduled_at = _iso(stop_at)
        # the safety net: the pod goes at the earlier limit even when this process does not get to remove it
        lium.schedule_termination(result.pod_id, termination_time=result.removal_scheduled_at)

        def left() -> float:
            return (stop_at - datetime.now(timezone.utc)).total_seconds()

        try:
            pod = lium.wait_ready(rented.pod, timeout=max(1, int(min(boot_timeout, left()))))
        except PodStartError as exc:
            result.status, result.error = BOOT_FAILED, str(exc)
            return result
        if pod is None:
            result.status = stopped_by if left() <= 1 else BOOT_FAILED
            result.error = "the pod did not become ready in time"
            return result

        for local, remote in (inputs or {}).items():
            lium.upload(pod, local=local, remote=remote)

        run_s = left() - output_reserve_s
        if run_s < 1:
            result.status, result.error = stopped_by, "no time left to run the command"
            return result
        remote = f"timeout -k 10 {math.floor(run_s)} bash -c {shlex.quote(command)}"
        try:
            run = lium.exec(pod, command=remote, env=env, timeout=run_s + 30)
        except TimeoutError as exc:
            result.status, result.error = stopped_by, str(exc)
        else:
            result.exit_code = run.get("exit_code")
            result.stdout, result.stderr = run.get("stdout", ""), run.get("stderr", "")
            if result.exit_code == 0:
                result.status = SUCCEEDED
            elif result.exit_code in _TIMEOUT_EXITS:
                result.status = stopped_by
            else:
                result.status = FAILED

        # copied after a failure or a timeout too: a partial log or checkpoint is often what the caller needs
        for remote_path, local in (outputs or {}).items():
            try:
                lium.download(pod, remote=remote_path, local=local)
                result.outputs[remote_path] = local
            except Exception as exc:  # noqa: BLE001 - one missing output must not lose the others
                result.missing_outputs[remote_path] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - reported as the job's terminal state, the pod is still removed
        result.status, result.error = ERROR, str(exc)
        return result
    finally:
        if result.pod_id:
            result.cleanup = _remove(lium, result.pod_id)
            ended = datetime.now(timezone.utc)
            if result.cleanup == "scheduled":
                ended = max(ended, stop_at)
            result.estimated_cost_usd = round(
                (result.price_per_hour or 0.0) * (ended - rented_at).total_seconds() / 3600, 6
            )
            result.cost_usd = _billed(lium, result.pod_id, rented_at)
        result.seconds = round(time.monotonic() - started, 3)


def _remove(lium: "Lium", pod_id: str) -> str:
    """Delete the pod, retrying a refusal (a pod still deploying cannot be deleted yet)."""
    for attempt in range(_DELETE_ATTEMPTS):
        try:
            lium._request("DELETE", f"/pods/{pod_id}")
            return "removed"
        except Exception:  # noqa: BLE001 - the scheduled removal is the fallback
            if attempt + 1 < _DELETE_ATTEMPTS:
                time.sleep(5 * (attempt + 1))
    return "scheduled"


def _remove_by_name(lium: "Lium", name: str) -> str:
    try:
        pods = lium.ps()
    except Exception:  # noqa: BLE001 - cannot tell whether a pod exists
        return "unknown"
    states: List[str] = [_remove(lium, pod.id) for pod in pods if pod.name == name]
    if not states:
        return "not_rented"
    return "removed" if all(s == "removed" for s in states) else "unknown"


def _billed(lium: "Lium", pod_id: str, rented_at: datetime) -> Optional[float]:
    try:
        statement = lium.billing_statement(
            start_day=rented_at.strftime("%Y-%m-%d"),
            end_day=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        )
    except Exception:  # noqa: BLE001 - the estimate stands
        return None
    for row in statement.get("pods") or []:
        if str(row.get("pod_id")) == pod_id and row.get("total") is not None:
            return float(row["total"])
    return None
