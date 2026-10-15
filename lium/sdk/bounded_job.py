"""One bounded GPU job: rent by spec, run a command, copy the outputs back, remove the pod.

The caller states the job (a command, files in, files out), the hardware (a GPU type and count,
plus any :meth:`Lium.rent` constraint), an approximate budget in USD and a deadline in seconds.
Right after the rent the pod's removal is scheduled server-side for the earlier of the two limits;
once that schedule is acknowledged, removal survives this process exiting. A crash during the rent
or the scheduling call can leave a billable pod without a removal bound. The ``finally`` block
deletes the pod and waits until it is gone from the pod list. Limits are best effort: the server
processes scheduled removals periodically, so charges can exceed the budget.
The result names a terminal status, the exit code, the outputs copied, how the pod was cleaned
up and what it cost.
"""

from __future__ import annotations

import math
import shlex
import threading
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

# `timeout` exits 124 when its TERM stopped the command, 137 when the KILL after `-k` had to;
# the command can exit with these itself, so they only count as a timeout once the limit has passed
_TIMEOUT_EXITS = (124, 137)
_DELETE_ATTEMPTS = 3
_CONFIRM_S = 60  # how long cleanup waits for a deleted pod to leave the pod list
_POLL_S = 5


@dataclass
class JobResult:
    """What :meth:`Lium.run_job` did. ``to_dict()`` is JSON-ready.

    ``cleanup`` is ``"removed"`` (the pod was deleted and is gone from the pod list),
    ``"scheduled"`` (its absence was not confirmed; the acknowledged server-side removal at
    ``removal_scheduled_at`` stands), ``"not_rented"``, or ``"unknown"`` (neither removal nor a
    schedule was confirmed: check :meth:`Lium.ps`). ``removal_scheduled_at`` is set only once the
    server acknowledged the schedule. ``cost_usd`` is what the billing statement shows for the pod,
    read only after a confirmed removal (``None`` otherwise, or when the statement does not list it
    yet); ``estimated_cost_usd`` is the rent's hourly price times the seconds from just before the
    rent request to removal (to ``removal_scheduled_at`` when removal is unconfirmed).
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
    # billing starts when the server creates the pod, before the response arrives: every limit,
    # estimate and statement range is anchored to this moment, taken before the rent request
    rented_at = datetime.now(timezone.utc)
    deadline_at = rented_at + timedelta(seconds=deadline_s)
    # a per-call suffix even on a caller's name, so only this call's pod can ever match it
    result = JobResult(status=ERROR, name=f"{name or 'job'}-{uuid.uuid4().hex[:8]}")
    stop_at = deadline_at
    try:
        try:
            rented = lium.rent(gpu_type=gpu_type, gpu_count=gpu_count, name=result.name, **rent_kwargs)
        except Exception as exc:  # noqa: BLE001 - every rent failure is this job's terminal state
            result.status, result.error = RENT_FAILED, str(exc)
            # the server may have created the pod before the response was lost; the name is this call's own
            result.cleanup = _remove_by_name(lium, result.name)
            return result

        result.pod_id = rented.pod["id"]
        result.price_per_hour = rented.price_per_hour
        if rented.price_per_hour and rented.price_per_hour > 0:
            budget_at = rented_at + timedelta(hours=max_cost_usd / rented.price_per_hour)
            stop_at = min(deadline_at, budget_at)
        stopped_by = BUDGET_EXHAUSTED if stop_at < deadline_at else TIMED_OUT
        # the safety net: the pod goes at the earlier limit even when this process does not get to remove it
        lium.schedule_termination(result.pod_id, termination_time=_iso(stop_at))
        result.removal_scheduled_at = _iso(stop_at)  # only once acknowledged

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
            _bounded(lambda local=local, remote=remote: lium.upload(pod, local=local, remote=remote), left(), f"upload of {local}")

        run_s = left() - output_reserve_s
        if run_s < 1:
            result.status, result.error = stopped_by, "no time left to run the command"
            return result
        limit_s = math.floor(run_s)
        remote = f"timeout -k 10 {limit_s} bash -c {shlex.quote(command)}"
        exec_started = time.monotonic()
        try:
            run = lium.exec(pod, command=remote, env=env, timeout=run_s + 30)
        except TimeoutError as exc:
            result.status, result.error = stopped_by, str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. OutputLimitExceeded (lium#314): the outputs below still matter
            result.status, result.error = ERROR, str(exc)
        else:
            result.exit_code = run.get("exit_code")
            result.stdout, result.stderr = run.get("stdout", ""), run.get("stderr", "")
            if result.exit_code == 0:
                result.status = SUCCEEDED
            elif result.exit_code in _TIMEOUT_EXITS and time.monotonic() - exec_started >= limit_s:
                result.status = stopped_by
            else:
                result.status = FAILED

        # copied after a failure, an exec error or a timeout too: a partial log or checkpoint is what the caller needs
        for remote_path, local in (outputs or {}).items():
            try:
                _bounded(lambda remote_path=remote_path, local=local: lium.download(pod, remote=remote_path, local=local), left(), f"download of {remote_path}")
                result.outputs[remote_path] = local
            except Exception as exc:  # noqa: BLE001 - one missing output must not lose the others
                result.missing_outputs[remote_path] = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - reported as the job's terminal state, the pod is still removed
        result.status, result.error = ERROR, str(exc)
        return result
    finally:
        if result.pod_id:
            if _remove(lium, result.pod_id):
                result.cleanup = "removed"
            else:
                if result.removal_scheduled_at is None:
                    try:  # idempotent; the job may have failed before the first schedule was acknowledged
                        lium.schedule_termination(result.pod_id, termination_time=_iso(stop_at))
                        result.removal_scheduled_at = _iso(stop_at)
                    except Exception:  # noqa: BLE001 - reported as cleanup "unknown"
                        pass
                result.cleanup = "scheduled" if result.removal_scheduled_at else "unknown"
            ended = datetime.now(timezone.utc)
            if result.cleanup == "scheduled":
                ended = max(ended, stop_at)
            result.estimated_cost_usd = round(
                (result.price_per_hour or 0.0) * (ended - rented_at).total_seconds() / 3600, 6
            )
            if result.cleanup == "removed":  # before that the statement holds charges still accruing
                result.cost_usd = _billed(lium, result.pod_id, rented_at)
        result.seconds = round(time.monotonic() - started, 3)


def _bounded(transfer, seconds: float, what: str) -> None:
    """Run ``transfer`` with a total deadline. SFTP has no transfer deadline of its own, so a stalled
    transfer is left on a daemon thread; the pod's removal right after closes its connection."""
    if seconds <= 0:
        raise TimeoutError(f"no time left for the {what}")
    box: Dict[str, BaseException] = {}

    def target() -> None:
        try:
            transfer()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["exc"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise TimeoutError(f"the {what} did not finish within {seconds:.0f}s")
    if "exc" in box:
        raise box["exc"]


def _remove(lium: "Lium", pod_id: str) -> bool:
    """Delete the pod, retrying a refusal (a pod still deploying cannot be deleted yet), then wait
    until it is gone from the pod list: a successful DELETE only starts the removal."""
    for attempt in range(_DELETE_ATTEMPTS):
        try:
            lium._request("DELETE", f"/pods/{pod_id}")
            break
        except Exception:  # noqa: BLE001 - the scheduled removal is the fallback
            if attempt + 1 < _DELETE_ATTEMPTS:
                time.sleep(5 * (attempt + 1))
    else:
        return False
    for _ in range(_CONFIRM_S // _POLL_S):
        try:
            if all(pod.id != pod_id for pod in lium.ps()):
                return True
        except Exception:  # noqa: BLE001 - not confirmed yet
            pass
        time.sleep(_POLL_S)
    return False


def _remove_by_name(lium: "Lium", name: str) -> str:
    try:
        pods = lium.ps()
    except Exception:  # noqa: BLE001 - cannot tell whether a pod exists
        return "unknown"
    removed: List[bool] = [_remove(lium, pod.id) for pod in pods if pod.name == name]
    if not removed:
        return "not_rented"
    return "removed" if all(removed) else "unknown"


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
