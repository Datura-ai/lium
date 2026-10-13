"""The tools `lium mcp` serves: thin calls into `lium.sdk.Lium` that return plain JSON-safe dicts.

An exception never reaches the MCP host as a traceback: every tool returns ``{"ok": false, "error": {code, message,
hint}}`` instead, so the model can read what went wrong and what to do next.
"""

import asyncio
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from lium.sdk import Lium
from lium.sdk.exceptions import LiumError, PodStartError
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
    """One rent_pod call, shared by the worker thread and the async tool that may be cancelled while it runs."""

    def __init__(self):
        self.lock = threading.Lock()
        self.cancelled = False
        self.finished = False


def _rented_pod_id(result: Optional[Dict[str, Any]]) -> Optional[str]:
    result = result or {}
    return result.get("pod_id") or (result.get("pod") or {}).get("pod_id")


def _tail(text: Optional[str]) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_TAIL_CHARS else f"…[cut to the last {OUTPUT_TAIL_CHARS} characters]\n" + text[-OUTPUT_TAIL_CHARS:]


class LiumTools:
    """One Lium client for the whole session, built on first use so `lium mcp` starts even without a key."""

    def __init__(self, client_factory: Callable[[], Lium] = lambda: Lium(source="mcp")):
        self._factory = client_factory
        self._client: Optional[Lium] = None
        # rentals the host cancelled whose pod is still being removed; a new rental waits for them
        self._cleanup = threading.Condition()
        self._cleanups_pending = 0

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

    CLEANUP_WAIT_SECONDS = 180
    CLEANUP_LIST_ATTEMPTS = 30
    CLEANUP_LIST_DELAY_SECONDS = 2.0

    def rent_pod(
        self,
        executor_id: str,
        name: str,
        template_id: Optional[str] = None,
        timeout_seconds: int = 600,
        rental: Optional[_Rental] = None,
    ) -> Dict[str, Any]:
        rental = rental or _Rental()

        def call():
            with self._cleanup:
                if not self._cleanup.wait_for(lambda: self._cleanups_pending == 0, self.CLEANUP_WAIT_SECONDS):
                    return _error(
                        "cleanup_pending",
                        "A cancelled rental is still being removed.",
                        "Call list_pods, remove_pod any pod you did not mean to keep, then rent again.",
                    )

            # rent and wait separately: once the rent call returns the pod bills, so a slow start must hand back its id
            # rather than an error the model would answer by renting a second pod
            created = self.client.up(executor_id=executor_id, name=name, template_id=template_id, wait=False)
            pod_id = created.get("id") if isinstance(created, dict) else getattr(created, "id", None)
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
                return {"ok": False, "pod_id": pod_id}
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

        result = _guarded(call)
        with rental.lock:
            rental.finished = True
            if not rental.cancelled:
                return result
        # the host cancelled the call and will never see this pod id: remove the pod so a retry is not a second bill
        self._discard(_rented_pod_id(result))
        return _error("cancelled", "The rental was cancelled; its pod was removed.")

    def cancel_rental(self, rental: _Rental, result: Optional[Dict[str, Any]] = None) -> None:
        """The host cancelled rent_pod. A worker still running removes its pod when it finishes; a finished one's
        result (which the host never received) is removed here. A new rental waits until removal is done."""
        with self._cleanup:
            self._cleanups_pending += 1
        with rental.lock:
            if not rental.finished:
                rental.cancelled = True
                return
        threading.Thread(target=self._discard, args=(_rented_pod_id(result),), daemon=True).start()

    def _discard(self, pod_id: Optional[str]) -> None:
        try:
            for attempt in range(self.CLEANUP_LIST_ATTEMPTS if pod_id else 0):
                target = next((p for p in self.client.ps() if p.id == pod_id), None)
                if target is not None:
                    self.client.rm(target)
                    return
                time.sleep(self.CLEANUP_LIST_DELAY_SECONDS)
        except Exception:  # noqa: BLE001 - best effort; list_pods still shows the pod
            pass
        finally:
            with self._cleanup:
                self._cleanups_pending -= 1
                self._cleanup.notify_all()

    def list_pods(self) -> Dict[str, Any]:
        return _guarded(lambda: {"ok": True, "pods": [_pod(p) for p in self.client.ps()]})

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
        work = asyncio.ensure_future(
            asyncio.to_thread(tools.rent_pod, executor_id, name, template_id, timeout_seconds, rental)
        )
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # the thread runs on; whatever pod it rents is removed, since the host will never learn its id
            tools.cancel_rental(rental, work.result() if work.done() and not work.cancelled() else None)
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
