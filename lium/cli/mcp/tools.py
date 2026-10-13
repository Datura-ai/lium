"""The tools `lium mcp` serves: thin calls into `lium.sdk.Lium` that return plain JSON-safe dicts.

An exception never reaches the MCP host as a traceback: every tool returns ``{"ok": false, "error": {code, message,
hint}}`` instead, so the model can read what went wrong and what to do next.
"""

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

from lium.sdk import Lium
from lium.sdk.exceptions import LiumError, LiumServerError, PodStartError
from lium.sdk.models import ExecutorInfo, PodInfo

# exec output is cut to its tail: a training log of megabytes would fill the model's context
OUTPUT_TAIL_CHARS = 20_000


def _error(code: str, message: str, hint: Optional[str] = None) -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


def _guarded(call: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    try:
        return call()
    except LiumError as e:
        return _error(e.code or type(e).__name__, str(e), e.hint)
    except Exception as e:  # noqa: BLE001 - the host must get an answer, not a dead stdio session
        return _error(type(e).__name__, str(e))


def _node(node: ExecutorInfo) -> Dict[str, Any]:
    location = node.location or {}
    return {
        "executor_id": node.id,
        "huid": node.huid,
        "gpu_type": node.gpu_type,
        "gpu_count": node.gpu_count,
        "available_gpu_count": node.available_gpu_count,
        "price_per_gpu_hour_usd": node.price_per_gpu,
        "price_per_hour_usd": node.price_per_hour,
        "country": location.get("country") or location.get("country_code"),
        "tier": node.tier,
        "max_cuda_version": node.max_cuda_version,
    }


def _pod(pod: PodInfo) -> Dict[str, Any]:
    return {
        "pod_id": pod.id,
        "name": pod.name,
        "huid": pod.huid,
        "status": pod.status,
        "ssh_cmd": pod.ssh_cmd,
        "gpu_type": pod.executor.gpu_type if pod.executor else None,
        "gpu_count": pod.gpu_count or (pod.executor.gpu_count if pod.executor else None),
        "price_per_hour_usd": pod.executor.price_per_hour if pod.executor else None,
        "created_at": pod.created_at,
    }


def _redact(text: Optional[str], command: str) -> Optional[str]:
    """`text` with the command cut out: commands often carry secrets (HF_TOKEN=... python train.py)."""
    return text.replace(command, "<command>") if text and command else text


class _RentalCancelled(Exception):
    pass


class _Rental:
    """One rent_pod call, shared by its worker thread and the async tool, which the host may cancel at any point.

    Once ``submitted`` (``up`` was called) a pod may exist. ``outcome`` is None while ``up`` is in flight, then
    "created", "rejected" (the API refused: no pod) or "unknown" (no answer: maybe a pod). ``pod_ids`` are the pods
    this rental made that are not yet confirmed removed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.name = ""
        self.cancelled = False
        self.submitted = False
        self.outcome: Optional[str] = None
        self.settled_at = 0.0
        self.before: Set[str] = set()
        self.pod_ids: Set[str] = set()
        self.removed: Set[str] = set()
        self.last_error: Optional[str] = None

    def report(self) -> Dict[str, Any]:
        with self.lock:
            return {"name": self.name, "pod_ids": sorted(self.pod_ids), "outcome": self.outcome, "last_error": self.last_error}


def _tail(text: Optional[str]) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_TAIL_CHARS else f"…[cut to the last {OUTPUT_TAIL_CHARS} characters]\n" + text[-OUTPUT_TAIL_CHARS:]


class LiumTools:
    """One Lium client for the whole session, built on first use so `lium mcp` starts even without a key."""

    def __init__(self, client_factory: Callable[[], Lium] = lambda: Lium(source="mcp")):
        self._factory = client_factory
        self._client: Optional[Lium] = None
        # Rentals whose result the host never got (it cancelled, or the rent call went unanswered). Each stays here
        # until its pod is confirmed removed or confirmed never created; while any does, rent_pod refuses to rent.
        self._cleanup = threading.Condition()
        self._unresolved: List[_Rental] = []
        self._reconciling = threading.Lock()
        self._reconciler_running = False

    @property
    def client(self) -> Lium:
        if self._client is None:
            self._client = self._factory()
        return self._client

    def _find_pod(self, pod: str) -> PodInfo:
        """The one pod `pod` names. An exact id wins; a huid or name must match exactly one pod, so the target
        never depends on listing order (names are free text and need not be unique)."""
        pods = self.client.ps()
        for candidate in pods:
            if candidate.id == pod:
                return candidate
        matches = {c.id: c for c in pods if pod in (c.huid, c.name)}
        if len(matches) == 1:
            return next(iter(matches.values()))
        if matches:
            raise LookupError(
                f"'{pod}' matches {len(matches)} pods ({', '.join(sorted(matches))}); pass the pod_id of the one you mean."
            )
        raise LookupError(f"No pod '{pod}' among your {len(pods)} pods; call list_pods for their ids and names.")

    def list_gpus(self, gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        def call():
            nodes = self.client.ls(gpu_type=gpu_type, gpu_count=gpu_count)
            nodes = sorted(nodes, key=lambda n: n.price_per_gpu)[: max(1, limit)]
            return {"ok": True, "billing": "per second at the hourly price, no minimum", "nodes": [_node(n) for n in nodes]}

        return _guarded(call)

    CLEANUP_WAIT_SECONDS = 30  # how long rent_pod waits for an earlier rental's cleanup before refusing
    CLEANUP_RETRY_SECONDS = 2.0  # first background retry; doubles up to CLEANUP_RETRY_MAX_SECONDS
    CLEANUP_RETRY_MAX_SECONDS = 30.0
    NEVER_LISTED_SECONDS = 120  # a pod not listed this long after its rent call settled was never created
    BACKGROUND_CLEANUP = True

    def rent_pod(
        self,
        executor_id: str,
        name: str,
        template_id: Optional[str] = None,
        timeout_seconds: int = 600,
        rental: Optional[_Rental] = None,
    ) -> Dict[str, Any]:
        rental = rental or _Rental()
        not_rented = _error("cancelled", "The rental was cancelled before it was sent; nothing was rented.")

        def call():
            deadline = time.monotonic() + self.CLEANUP_WAIT_SECONDS
            while True:
                unresolved = self._reconcile()
                if not unresolved:
                    break
                if rental.cancelled:
                    return not_rented
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._cleanup_pending_error(unresolved)
                with self._cleanup:
                    if not rental.cancelled:
                        self._cleanup.wait(min(remaining, self.CLEANUP_RETRY_SECONDS))

            before = {p.id for p in self.client.ps()}
            with rental.lock:
                if rental.cancelled:
                    return not_rented
                rental.name, rental.before, rental.submitted = name, before, True

            # rent and wait separately: once the rent call returns the pod bills, so a slow start must hand back its id
            # rather than an error the model would answer by renting a second pod
            try:
                created = self.client.up(executor_id=executor_id, name=name, template_id=template_id, wait=False)
            except LiumError as e:
                if not isinstance(e, LiumServerError):
                    self._settle(rental, "rejected")
                    raise
                self._settle(rental, "unknown")
                self._track(rental)
                return self._unknown_outcome_error(name, e)
            except Exception as e:  # noqa: BLE001 - no answer: the pod may exist
                self._settle(rental, "unknown")
                self._track(rental)
                return self._unknown_outcome_error(name, e)

            pod_id = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
            self._settle(rental, "created" if pod_id else "unknown", pod_id)
            if not pod_id:
                self._track(rental)
                return self._unknown_outcome_error(name, ValueError("the rent call returned no pod id"))
            pending = {
                "ok": True,
                "status": "pending",
                "pod_id": pod_id,
                "name": name,
                "next": "The pod is rented and billing. Do NOT call rent_pod again: call list_pods until its status is "
                "RUNNING, then run_command; or remove_pod with this pod_id to stop billing.",
            }

            def stop_if_cancelled(*_):
                if rental.cancelled:
                    raise _RentalCancelled()

            try:
                stop_if_cancelled()
                ready = self.client.wait_ready(created, timeout=timeout_seconds, on_poll=stop_if_cancelled)
            except _RentalCancelled:
                return _error("cancelled", f"The rental was cancelled; pod {pod_id} is being removed.")
            except PodStartError as e:
                failed = _error(
                    "pod_start_failed",
                    f"Pod {pod_id} will not start (status {e.status}): {e}",
                    "Call remove_pod with this pod_id so it cannot bill, then rent a different node.",
                )
                failed["pod_id"] = pod_id
                return failed
            except Exception as e:  # noqa: BLE001 - the rent went through; losing its id would cause a second rental
                return {**pending, "warning": f"Could not confirm the pod is ready: {type(e).__name__}: {e}"}
            if ready is None:
                return {**pending, "warning": f"Not running after {timeout_seconds}s; some nodes take longer to start."}
            return {"ok": True, "pod": _pod(ready), "next": "run_command on this pod; remove_pod when done (billing stops)"}

        return _guarded(call)

    def cancel_rental(self, rental: _Rental) -> None:
        """The host cancelled rent_pod and will never see its result. A rental not yet sent stops before renting; a sent
        one is tracked until its pod is confirmed removed, whatever stage the worker thread has reached."""
        with rental.lock:
            rental.cancelled = True
            submitted = rental.submitted
        if submitted:
            self._track(rental)
        with self._cleanup:
            self._cleanup.notify_all()

    @staticmethod
    def _settle(rental: _Rental, outcome: str, pod_id: Optional[str] = None) -> None:
        with rental.lock:
            rental.outcome, rental.settled_at = outcome, time.monotonic()
            if pod_id:
                rental.pod_ids.add(pod_id)

    def _track(self, rental: _Rental) -> None:
        with self._cleanup:
            if rental not in self._unresolved:
                self._unresolved.append(rental)
            start = self.BACKGROUND_CLEANUP and not self._reconciler_running
            self._reconciler_running = self._reconciler_running or start
        if start:
            threading.Thread(target=self._reconcile_in_background, daemon=True).start()

    def _reconcile_in_background(self) -> None:
        delay = self.CLEANUP_RETRY_SECONDS
        while True:
            self._reconcile()
            with self._cleanup:
                if not self._unresolved:
                    self._reconciler_running = False
                    return
            time.sleep(delay)
            delay = min(delay * 2, self.CLEANUP_RETRY_MAX_SECONDS)

    def _reconcile(self) -> List[_Rental]:
        """Try once to settle every unresolved rental; returns those still unresolved."""
        with self._cleanup:
            if not self._unresolved:
                return []
        with self._reconciling:
            with self._cleanup:
                pending = list(self._unresolved)
            try:
                live = {p.id: p for p in self.client.ps()}
            except Exception as e:  # noqa: BLE001 - nothing is confirmed; try again later
                for rental in pending:
                    with rental.lock:
                        rental.last_error = f"listing pods failed: {type(e).__name__}: {e}"
                return pending
            resolved = [rental for rental in pending if self._settle_cleanup(rental, live)]
            with self._cleanup:
                self._unresolved = [r for r in self._unresolved if r not in resolved]
                self._cleanup.notify_all()
                return list(self._unresolved)

    def _settle_cleanup(self, rental: _Rental, live: Dict[str, PodInfo]) -> bool:
        """True once nothing this rental made can still bill: its pods are removed (rm acknowledged), the API
        rejected it, or a pod it may have made was never listed."""
        with rental.lock:
            outcome, settled_at = rental.outcome, rental.settled_at
            if outcome is None:
                rental.last_error = "the rent call has not returned yet"
                return False
            if outcome == "rejected":
                return True
            if outcome == "unknown":
                rental.pod_ids |= {p.id for p in live.values() if p.name == rental.name and p.id not in rental.before}
            pod_ids = set(rental.pod_ids)
        never_listed = time.monotonic() - settled_at >= self.NEVER_LISTED_SECONDS
        if not pod_ids:
            if outcome == "unknown" and not never_listed:
                with rental.lock:
                    rental.last_error = "waiting to see whether the rent call created a pod"
                return False
            return True
        for pod_id in pod_ids:
            target = live.get(pod_id)
            try:
                if target is None:
                    if not never_listed:
                        raise LookupError(f"pod {pod_id} is not listed yet")
                else:
                    self.client.rm(target)
            except Exception as e:  # noqa: BLE001 - e.g. the API refuses to delete a pod still PENDING; retry later
                with rental.lock:
                    rental.last_error = f"removing {pod_id} failed: {type(e).__name__}: {e}"
                continue
            with rental.lock:
                rental.pod_ids.discard(pod_id)
                if target is not None:
                    rental.removed.add(pod_id)
        with rental.lock:
            return not rental.pod_ids

    def _cleanup_pending_error(self, unresolved: List[_Rental]) -> Dict[str, Any]:
        failed = _error(
            "cleanup_pending",
            f"{len(unresolved)} earlier rental(s) whose result never reached you may still be billing; renting again "
            "waits until they are removed.",
            "Retry rent_pod in a minute, or call list_pods and remove_pod the pod ids listed under unresolved.",
        )
        failed["unresolved"] = [rental.report() for rental in unresolved]
        return failed

    @staticmethod
    def _unknown_outcome_error(name: str, e: Exception) -> Dict[str, Any]:
        return _error(
            "rent_outcome_unknown",
            f"The rent call got no clear answer ({type(e).__name__}: {e}); a pod named '{name}' may have been created.",
            "Any pod it created is removed automatically; rent_pod works again once that is confirmed (see list_pods).",
        )

    def list_pods(self) -> Dict[str, Any]:
        def call():
            result = {"ok": True, "pods": [_pod(p) for p in self.client.ps()]}
            with self._cleanup:
                unresolved = list(self._unresolved)
            if unresolved:
                result["unresolved_rentals"] = [rental.report() for rental in unresolved]
            return result

        return _guarded(call)

    def run_command(self, pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        def call():
            target = self._find_pod(pod)
            try:
                result = self.client.exec(target, command=command, timeout=timeout_seconds)
            except TimeoutError:
                # the SDK's message repeats the command, which may hold a secret: say it without the command
                return _error(
                    "command_timeout",
                    f"The command did not finish within {timeout_seconds}s on pod {target.id}.",
                    "Raise timeout_seconds, or start long jobs in the background (nohup ... > log 2>&1 &) and read the log.",
                )
            except LiumError as e:
                return _error(e.code or type(e).__name__, _redact(str(e), command), _redact(e.hint, command))
            except Exception as e:  # noqa: BLE001
                return _error(type(e).__name__, _redact(str(e), command))
            return {
                "ok": True,
                "exit_code": result.get("exit_code"),
                "stdout": _tail(result.get("stdout")),
                "stderr": _tail(result.get("stderr")),
            }

        return _guarded(call)

    def remove_pod(self, pod: str) -> Dict[str, Any]:
        def call():
            target = self._find_pod(pod)
            self.client.rm(target)
            with self._cleanup:
                unresolved = list(self._unresolved)
            for rental in unresolved:  # a confirmed removal settles an unresolved rental's pod
                with rental.lock:
                    if target.id in rental.pod_ids:
                        rental.pod_ids.discard(target.id)
                        rental.removed.add(target.id)
            self._reconcile()
            return {"ok": True, "removed": target.id, "name": target.name}

        return _guarded(call)

    def get_balance(self) -> Dict[str, Any]:
        return _guarded(lambda: {"ok": True, "balance_usd": self.client.balance()})


def tool_functions(tools: LiumTools) -> List[Callable[..., Awaitable[Dict[str, Any]]]]:
    """The functions the server registers; their names and docstrings are what the model sees.

    They are async and run each blocking SDK call in a worker thread: mcp 1.x calls a sync tool inside its event loop,
    so a slow rental would otherwise stall every other request, including the remove_pod that stops billing."""

    async def list_gpus(gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        """Find GPU nodes for rent on Lium, cheapest first. Use before rent_pod to pick an executor_id.
        gpu_type filters by model ("H100", "RTX 4090", "B200"); gpu_count by GPUs on the node."""
        return await asyncio.to_thread(tools.list_gpus, gpu_type, gpu_count, limit)

    async def rent_pod(executor_id: str, name: str, template_id: Optional[str] = None, timeout_seconds: int = 600) -> Dict[str, Any]:
        """Rent a GPU pod on a node from list_gpus and wait until it is running. This spends money: the pod is
        billed per second at the node's hourly price until remove_pod. Give it a short unique name. If the result
        has status "pending", the pod is already rented: do not rent again, poll list_pods or remove_pod its pod_id."""
        rental = _Rental()
        try:
            return await asyncio.to_thread(tools.rent_pod, executor_id, name, template_id, timeout_seconds, rental)
        except asyncio.CancelledError:
            # the thread runs on; whatever pod it rents is removed, since the host will never learn its id
            tools.cancel_rental(rental)
            raise

    async def list_pods() -> Dict[str, Any]:
        """List your pods with their status, GPU, price per hour and SSH command."""
        return await asyncio.to_thread(tools.list_pods)

    async def run_command(pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        """Run a shell command on a running pod (by pod id, or a huid or name that matches one pod) and return exit
        code, stdout and stderr."""
        return await asyncio.to_thread(tools.run_command, pod, command, timeout_seconds)

    async def remove_pod(pod: str) -> Dict[str, Any]:
        """Remove a pod (by pod id, or a huid or name that matches one pod). Billing stops; anything on its disk is gone."""
        return await asyncio.to_thread(tools.remove_pod, pod)

    async def get_balance() -> Dict[str, Any]:
        """Your Lium account balance in USD."""
        return await asyncio.to_thread(tools.get_balance)

    return [list_gpus, rent_pod, list_pods, run_command, remove_pod, get_balance]
