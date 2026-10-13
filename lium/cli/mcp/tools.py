"""The tools `lium mcp` serves: thin calls into `lium.sdk.Lium` that return plain JSON-safe dicts.

An exception never reaches the MCP host as a traceback: every tool returns ``{"ok": false, "error": {code, message,
hint}}`` instead, so the model can read what went wrong and what to do next.
"""

from typing import Any, Callable, Dict, List, Optional

from lium.sdk import Lium
from lium.sdk.exceptions import LiumError
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
        pods = self.client.ps()
        for candidate in pods:
            if pod in (candidate.id, candidate.huid, candidate.name):
                return candidate
        raise LookupError(f"No pod '{pod}' among your {len(pods)} pods; call list_pods for their ids and names.")

    def list_gpus(self, gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        def call():
            nodes = self.client.ls(gpu_type=gpu_type, gpu_count=gpu_count)
            nodes = sorted(nodes, key=lambda n: n.price_per_gpu)[: max(1, limit)]
            return {"ok": True, "billing": "per second at the hourly price, no minimum", "nodes": [_node(n) for n in nodes]}

        return _guarded(call)

    def rent_pod(self, executor_id: str, name: str, template_id: Optional[str] = None, timeout_seconds: int = 600) -> Dict[str, Any]:
        def call():
            pod = self.client.up(executor_id=executor_id, name=name, template_id=template_id, wait=True, timeout=timeout_seconds)
            if isinstance(pod, PodInfo):
                return {"ok": True, "pod": _pod(pod), "next": "run_command on this pod; remove_pod when done (billing stops)"}
            return {"ok": True, "pod": pod, "next": "call list_pods until status is RUNNING, then run_command"}

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


def tool_functions(tools: LiumTools) -> List[Callable[..., Dict[str, Any]]]:
    """The functions the server registers; their names and docstrings are what the model sees."""

    def list_gpus(gpu_type: Optional[str] = None, gpu_count: Optional[int] = None, limit: int = 20) -> Dict[str, Any]:
        """Find GPU nodes for rent on Lium, cheapest first. Use before rent_pod to pick an executor_id.
        gpu_type filters by model ("H100", "RTX 4090", "B200"); gpu_count by GPUs on the node."""
        return tools.list_gpus(gpu_type, gpu_count, limit)

    def rent_pod(executor_id: str, name: str, template_id: Optional[str] = None, timeout_seconds: int = 600) -> Dict[str, Any]:
        """Rent a GPU pod on a node from list_gpus and wait until it is running. This spends money: the pod is
        billed per second at the node's hourly price until remove_pod. Give it a short unique name."""
        return tools.rent_pod(executor_id, name, template_id, timeout_seconds)

    def list_pods() -> Dict[str, Any]:
        """List your pods with their status, GPU, price per hour and SSH command."""
        return tools.list_pods()

    def run_command(pod: str, command: str, timeout_seconds: int = 300) -> Dict[str, Any]:
        """Run a shell command on a running pod (by pod id, huid or name) and return exit code, stdout and stderr."""
        return tools.run_command(pod, command, timeout_seconds)

    def remove_pod(pod: str) -> Dict[str, Any]:
        """Remove a pod (by pod id, huid or name). Billing stops; anything on its disk is gone."""
        return tools.remove_pod(pod)

    def get_balance() -> Dict[str, Any]:
        """Your Lium account balance in USD."""
        return tools.get_balance()

    return [list_gpus, rent_pod, list_pods, run_command, remove_pod, get_balance]
