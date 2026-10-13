"""The tools `lium mcp` serves: thin calls into `lium.sdk.Lium` that return plain JSON-safe dicts.

An exception never reaches the MCP host as a traceback: every tool returns ``{"ok": false, "error": {code, message,
hint}}`` instead, so the model can read what went wrong and what to do next.
"""

import asyncio
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


def _tail(text: Optional[str]) -> str:
    text = text or ""
    return text if len(text) <= OUTPUT_TAIL_CHARS else f"…[cut to the last {OUTPUT_TAIL_CHARS} characters]\n" + text[-OUTPUT_TAIL_CHARS:]


class LiumTools:
    """One Lium client for the whole session, built on first use so `lium mcp` starts even without a key."""

    def __init__(self, client_factory: Callable[[], Lium] = lambda: Lium(source="mcp")):
        self._factory = client_factory
        self._client: Optional[Lium] = None

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

    def rent_pod(self, executor_id: str, name: str, template_id: Optional[str] = None, timeout_seconds: int = 600) -> Dict[str, Any]:
        def call():
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
            try:
                ready = self.client.wait_ready(created, timeout=timeout_seconds)
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

    def list_pods(self) -> Dict[str, Any]:
        return _guarded(lambda: {"ok": True, "pods": [_pod(p) for p in self.client.ps()]})

    def run_command(self, pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        def call():
            result = self.client.exec(self._find_pod(pod), command=command, timeout=timeout_seconds)
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
        return await asyncio.to_thread(tools.rent_pod, executor_id, name, template_id, timeout_seconds)

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
