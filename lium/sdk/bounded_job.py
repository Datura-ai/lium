"""One bounded GPU job: rent by spec, run a command, copy the outputs back, remove the pod.

The caller states the job (a command, files in, files out), the hardware (a GPU type and count,
plus any :meth:`Lium.rent` constraint), an approximate budget in USD and a deadline in seconds.
Right after the rent the pod's removal is scheduled server-side for the earlier of the two limits;
once that schedule is acknowledged, removal survives this process exiting. An interruption
(Ctrl-C) during the rent still looks the pod up by its name and removes it; a hard kill (SIGKILL,
a lost machine) during the rent or the scheduling call can leave a billable pod without a removal
bound. The ``finally`` block
deletes the pod and waits until it is gone from the pod list. Limits are best effort: the server
processes scheduled removals periodically, so charges can exceed the budget.
The result names a terminal status, the exit code, the outputs copied, how the pod was cleaned
up and what it cost.
"""

from __future__ import annotations

import contextlib
import math
import os
import shlex
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import requests

from .exceptions import ClusterNotListedError, LiumRateLimitError, LiumServerError, PodStartError

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

# `timeout` exits 124 when its TERM stopped the command, 137 when the KILL after `-k` had to; the
# command can exit with these itself, so the pod prints this marker only when `timeout --verbose`
# reported sending the signal (to a file of its own, not to the command's stderr)
_TIMEOUT_EXITS = (124, 137)
_TIMED_OUT_MARK = "lium-run-job: the time limit stopped the command"
# the rent errors after which the server may still have created the pod (see Lium.rent)
_UNANSWERED = (requests.RequestException, LiumServerError, LiumRateLimitError, TimeoutError, ClusterNotListedError)
# error codes the API and SDK use for a request whose outcome is not known yet
_UNKNOWN_CODES = ("rent_outcome_unknown", "idempotency_in_progress", "idempotency_outcome_unknown")
_DELETE_ATTEMPTS = 3
_CONFIRM_S = 60  # wall-clock budget for one pod's cleanup: the DELETE, its retries and the pod-list checks
_POLL_S = 5


@dataclass
class JobResult:
    """What :meth:`Lium.run_job` did. ``to_dict()`` is JSON-ready.

    ``cleanup`` is ``"removed"`` (the pod was deleted and is gone from the pod list),
    ``"scheduled"`` (its absence was not confirmed; the acknowledged server-side removal at
    ``removal_scheduled_at`` stands), ``"not_rented"`` (the rent was refused), or ``"unknown"``
    (neither removal nor a schedule was confirmed, or a rent whose answer was lost may still have
    created a pod named ``name``: check :meth:`Lium.ps`). ``removal_scheduled_at`` is set only once the
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
    max_output_bytes: int = 16 << 20,
    max_download_bytes: int = 10 << 30,
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
            result.pod_id = rented.pod["id"]
        except BaseException as exc:  # noqa: BLE001 - every rent failure is this job's terminal state
            result.status, result.error = RENT_FAILED, str(exc) or type(exc).__name__
            # the server may have created the pod before the response was lost, or before a Ctrl-C
            # cut the wait for it; the name is this call's own
            interrupted = not isinstance(exc, Exception)
            result.cleanup = _remove_by_name(lium, result.name, unanswered=interrupted or _rent_may_exist(exc))
            if result.cleanup == "unknown":
                result.error += f"; the rent may still have created a pod named {result.name}: check Lium.ps()"
            if interrupted:
                raise
            return result

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
            if left() <= 0:
                raise TimeoutError(f"no time left for the upload of {local}")
            lium.upload(pod, local=local, remote=remote, timeout=left())

        run_s = left() - output_reserve_s
        if run_s < 1:
            result.status, result.error = stopped_by, "no time left to run the command"
            return result
        limit_s = math.floor(run_s)
        # timeout's own stderr goes to $f, the command gets the real stderr back through fd 3
        script = shlex.quote("exec 2>&3 3>&-\n" + command)
        remote = (
            f"{{ f=$(mktemp); timeout --verbose -k 10 {limit_s} bash -c {script} 3>&2 2>\"$f\"; rc=$?; "
            f"if grep -q '^timeout: sending signal' \"$f\"; then echo {shlex.quote(_TIMED_OUT_MARK)} >&2; "
            f"else cat \"$f\" >&2; fi; rm -f \"$f\"; exit $rc; }}"
        )
        try:
            run = lium.exec(pod, command=remote, env=env, timeout=run_s + 30, max_output_bytes=max_output_bytes)
        except TimeoutError as exc:
            result.status, result.error = stopped_by, str(exc)
        except Exception as exc:  # noqa: BLE001 - e.g. SSH dropped: the outputs below still matter
            result.status, result.error = ERROR, str(exc)
        else:
            result.exit_code = run.get("exit_code")
            result.stdout, result.stderr = run.get("stdout", ""), run.get("stderr", "") or ""
            mark = _TIMED_OUT_MARK + "\n"
            timed_out = result.exit_code in _TIMEOUT_EXITS and result.stderr.endswith(mark)
            if timed_out:
                result.stderr = result.stderr[: -len(mark)]
            if run.get("truncated"):
                result.error = f"stdout/stderr cut to their last {max_output_bytes} bytes each"
            if result.exit_code == 0:
                result.status = SUCCEEDED
            elif timed_out:
                result.status = stopped_by
            else:
                result.status = FAILED

        # copied after a failure, an exec error or a timeout too: a partial log or checkpoint is what the caller needs
        for remote_path, local in (outputs or {}).items():
            try:
                _download(lium, pod, remote_path, local, left(), max_download_bytes)
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
    """Run an API call with a total deadline; one that stalls past it is left on a daemon thread."""
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


def _rent_may_exist(exc: BaseException) -> bool:
    """Whether a failed rent may still have created the pod: its answer was lost, or the SDK says
    the outcome is unknown (a replay key on the error, or an unknown-outcome code)."""
    return (
        isinstance(exc, _UNANSWERED)
        or getattr(exc, "idempotency_key", None) is not None
        or getattr(exc, "code", None) in _UNKNOWN_CODES
    )


def _download(lium: "Lium", pod: Any, remote: str, local: str, seconds: float, max_bytes: int) -> None:
    """Download to a side file and move it onto ``local`` only once complete, so a transfer cut
    at the deadline or the size limit never leaves a partial file at the caller's path."""
    if seconds <= 0:
        raise TimeoutError(f"no time left for the download of {remote}")
    part = f"{local}.lium-part-{uuid.uuid4().hex[:8]}"
    try:
        lium.download(pod, remote=remote, local=part, max_bytes=max_bytes, timeout=seconds)
        os.replace(part, local)
    finally:
        with contextlib.suppress(OSError):  # gone after the move; a partial file otherwise
            os.remove(part)


def _remove(lium: "Lium", pod_id: str) -> bool:
    """Delete the pod, retrying a refusal (a pod still deploying cannot be deleted yet), then wait
    until it is gone from the pod list: a successful DELETE only starts the removal. Every request,
    retry and sleep is cut to what is left of ``_CONFIRM_S``, so a slow API cannot stretch it."""
    end = time.monotonic() + _CONFIRM_S

    def left() -> float:
        return end - time.monotonic()

    for attempt in range(_DELETE_ATTEMPTS):
        try:
            _bounded(lambda: lium._request("DELETE", f"/pods/{pod_id}"), left(), "pod deletion")
            break
        except Exception:  # noqa: BLE001 - the scheduled removal is the fallback
            if attempt + 1 < _DELETE_ATTEMPTS:
                time.sleep(max(0.0, min(5 * (attempt + 1), left())))
    else:
        return False
    while left() > 0:
        box: List[Any] = []
        try:
            _bounded(lambda: box.append(lium.ps()), left(), "pod listing")
            if all(pod.id != pod_id for pod in box[0]):
                return True
        except Exception:  # noqa: BLE001 - not confirmed yet
            pass
        time.sleep(max(0.0, min(_POLL_S, left())))
    return False


def _remove_by_name(lium: "Lium", name: str, *, unanswered: bool) -> str:
    try:
        pods = lium.ps()
    except Exception:  # noqa: BLE001 - cannot tell whether a pod exists
        return "unknown"
    removed: List[bool] = [_remove(lium, pod.id) for pod in pods if pod.name == name]
    if not removed:
        # an unanswered rent can still create the pod after this listing; only a refusal settles it
        return "unknown" if unanswered else "not_rented"
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
