"""Datamodels used across the Lium SDK."""

from dataclasses import dataclass, field, fields
import re
from typing import Any, Dict, List, Optional


class _Serializable:
    """``to_dict()`` for the models below (not ``RentResult`` or the workspace models), so a caller can ``json.dumps`` what the SDK returns.

    Nested dataclasses (a pod's executor) are converted too. Subclasses that
    derive useful values from their fields add them in ``_derived``.
    """

    def to_dict(self) -> Dict[str, Any]:
        data = {f.name: _to_plain(getattr(self, f.name)) for f in fields(self)}
        data.update(self._derived())
        return data

    def _derived(self) -> Dict[str, Any]:
        return {}


def _to_plain(value: Any) -> Any:
    if isinstance(value, _Serializable):
        return value.to_dict()
    if isinstance(value, dict):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


@dataclass
class ExecutorInfo(_Serializable):
    id: str
    huid: str
    machine_name: str
    gpu_type: str
    gpu_count: int
    # USD per hour for the whole node (price_per_gpu * gpu_count). A pod is
    # billed per second at this rate, with no minimum.
    price_per_hour: float
    price_per_gpu: float
    location: Dict
    specs: Dict
    status: str
    docker_in_docker: bool
    ip: str
    available_port_count: Optional[int] = None
    effective_upload_speed_mbps: Optional[float] = None
    effective_download_speed_mbps: Optional[float] = None
    max_cuda_version: Optional[float] = None
    tier: Optional[str] = None  # "spot" or "secure"; reclaim/penalty risk signal
    # GPUs free to rent on the node right now (the API's ``available_gpu_count``),
    # None when the API did not send it. ``gpu_count`` is the whole host; on a
    # partially rented split host this is the smaller number, and a rent that names
    # no count takes and is billed for exactly these.
    available_gpu_count: Optional[int] = None

    # How the GPUs are wired to each other, as the validator's `nvidia-smi topo` run saw them:
    # {"gpu_count", "gpu_pairs", "nvlink", "nvlink_links", "nvlink_pairs", "nvlink_active_links",
    #  "pcie_class", "p2p", "p2p_pairs", "p2p_ok_pairs", "matrix"}. None = not reported for this node.
    interconnect: Optional[Dict] = None
    # Every GPU pair on NVLink (an HGX board). False = PCIe and/or no peer-to-peer; None = unknown.
    nvlink: Optional[bool] = None

    @property
    def link(self) -> Optional[str]:
        """One-word interconnect label: ``NV18`` (NVLink, 18 links), ``PCIe/SYS`` (worst PCIe class), or None when unknown."""
        if self.nvlink is None:
            return None
        interconnect = self.interconnect or {}
        if self.nvlink:
            links = interconnect.get("nvlink_links")
            return f"NV{links}" if links else "NVLink"
        pcie_class = interconnect.get("pcie_class")
        return f"PCIe/{pcie_class}" if pcie_class else "PCIe"

    @property
    def p2p(self) -> Optional[bool]:
        """Every GPU pair can read each other's memory (NCCL works without NCCL_P2P_DISABLE=1); None when unknown."""
        value = (self.interconnect or {}).get("p2p")
        return value if isinstance(value, bool) else None

    @property
    def driver_version(self) -> str:
        """Extract GPU driver version from specs."""
        return self.specs.get("gpu", {}).get("driver", "")

    @property
    def gpu_model(self) -> str:
        """Extract GPU model name from specs."""
        gpu_details = self.specs.get("gpu", {}).get("details", [])
        return gpu_details[0].get("name", "") if gpu_details else ""

    @property
    def cpu_count(self) -> Optional[int]:
        """Number of CPU threads the node reports (``specs.cpu.count``); None if unknown."""
        try:
            return int((self.specs.get("cpu") or {}).get("count"))
        except (TypeError, ValueError):
            return None

    @property
    def download_speed(self) -> float:
        """Effective download speed in Mbps (backend-authoritative; 0.0 if unknown)."""
        return self.effective_download_speed_mbps or 0.0

    @property
    def upload_speed(self) -> float:
        """Effective upload speed in Mbps (backend-authoritative; 0.0 if unknown)."""
        return self.effective_upload_speed_mbps or 0.0

    def _derived(self) -> Dict[str, Any]:
        return {"gpu_model": self.gpu_model, "driver_version": self.driver_version}


@dataclass
class RentResult:
    """What :meth:`Lium.rent` chose and, unless it was a dry run, rented.

    ``pod`` is the same dict :meth:`Lium.up` returns (``id``, ``name``, ``executor_id``, …) and
    is ``None`` on a dry run. ``gpu_count`` is the GPUs rented and ``price_per_hour`` what the
    rental bills (``price_per_gpu`` × ``gpu_count``); both differ from ``executor.gpu_count`` /
    ``executor.price_per_hour`` when the server rents a split of a larger node.
    ``alternatives`` are the runners-up in the order they would have been tried; ``attempts`` is
    how many rents the server made before one succeeded (0 on a dry run, > 1 when the first pick
    was taken meanwhile); ``server_side`` says whether the backend chose the node or this client did.
    """

    executor: ExecutorInfo
    price_per_hour: float
    gpu_count: int = 1
    pod: Optional[Dict] = None
    template_id: Optional[str] = None
    candidates: int = 1
    alternatives: List[Dict] = field(default_factory=list)
    attempts: int = 0
    dry_run: bool = False
    server_side: bool = False


@dataclass
class PodInfo(_Serializable):
    id: str
    name: str
    status: str
    huid: str
    ssh_cmd: Optional[str]
    ports: Dict
    created_at: str
    updated_at: str
    executor: Optional[ExecutorInfo]
    template: Dict
    removal_scheduled_at: Optional[str]
    jupyter_installation_status: Optional[str]
    jupyter_url: Optional[str]
    enable_volume_encryption: bool | None = None
    volume_encryption_status: str | None = None
    # DAH-3005: set by the API only while the pod is PENDING / REBOOT_PENDING. Seconds left of the
    # backend's estimate (0 once it has run over), what the estimate rests on
    # (executor_history | class_median | cold_pull_estimate | fleet_default) and the creation step
    # (queued, preparing node, connecting to node, pulling image, creating volume, starting
    # container, mounting encrypted volume, configuring ssh). None on older backends and once RUNNING.
    estimated_ready_seconds: int | None = None
    eta_basis: str | None = None
    phase: str | None = None

    # GPUs this pod is billed for: the pod row's own ``gpu_count`` from ``/pods``,
    # None when the API did not send it. ``executor`` describes the whole host, so
    # for a GPU-split rental (2 of the host's 8) this is the smaller number.
    gpu_count: Optional[int] = None
    # The workspace the pod belongs to; None from a server without
    # workspaces or for a pod from before them.
    workspace_id: Optional[str] = None

    # Set on every member of a multi-node cluster rental; None on an ordinary pod.
    cluster_id: Optional[str] = None
    cluster_node_index: Optional[int] = None
    cluster_overlay_ip: Optional[str] = None

    # The API key that rented the pod (`api_key_id` / `api_key_name` on the /pods
    # row); None for a pod rented from the browser or listed by a server without the fields.
    api_key_id: Optional[str] = None
    api_key_name: Optional[str] = None
    # Whether the /pods row carried the key field at all (`api_key_id`, or the row's own
    # `created_by_api_key_id`, which prod sends as null for a browser rental). False only from a
    # server before per-key pods — the one case `ps --key` cannot tell one key's pods apart.
    api_key_stamped: bool = False

    def eta_hint(self) -> Optional[str]:
        """One line for a pod that is still starting, e.g. ``est. ready in ~18 s (phase: pulling image)``.

        ``None`` when the API sent neither an estimate nor a phase (older backend, or the pod is
        already RUNNING).
        """
        if self.estimated_ready_seconds is None and not self.phase:
            return None
        if self.estimated_ready_seconds is None:
            eta = None
        elif self.estimated_ready_seconds <= 0:
            eta = "est. ready any moment now"
        elif self.estimated_ready_seconds < 90:
            eta = f"est. ready in ~{self.estimated_ready_seconds} s"
        else:
            eta = f"est. ready in ~{round(self.estimated_ready_seconds / 60)} min"
        if not self.phase:
            return eta
        return f"{eta} (phase: {self.phase})" if eta else f"phase: {self.phase}"

    @property
    def host(self) -> Optional[str]:
        return (
            (re.findall(r"@(\S+)", self.ssh_cmd) or [None])[0] if self.ssh_cmd else None
        )

    @property
    def username(self) -> Optional[str]:
        return (
            (re.findall(r"ssh (\S+)@", self.ssh_cmd) or [None])[0]
            if self.ssh_cmd
            else None
        )

    @property
    def ssh_port(self) -> int:
        """Extract SSH port from command."""
        if not self.ssh_cmd or "-p " not in self.ssh_cmd:
            return 22
        return int(self.ssh_cmd.split("-p ")[1].split()[0])

    @property
    def volume_path(self) -> str:
        """Return the pod's local volume mount path."""
        volumes = self.template.get("volumes", []) if self.template else []
        return volumes[0] if volumes else "/root"

    @property
    def default_restore_path(self) -> str:
        """Return a safe restore destination below the local volume mount."""
        return f"{self.volume_path.rstrip('/')}/restored"

    def _derived(self) -> Dict[str, Any]:
        return {"host": self.host, "username": self.username, "ssh_port": self.ssh_port}


@dataclass
class ClusterOffer:
    """A group of free nodes on one RDMA fabric that can be rented as a single multi-node job.

    ``node_count`` is the whole fabric; ``nodes`` are the members free right now. Every node
    of a cluster rental is taken whole (all its GPUs), so the price of an N-node cluster is
    the sum of the N nodes' ``price_per_hour``.
    """

    fabric_id: str
    fabric_type: str  # "infiniband" or "roce"
    link_rate: Optional[str]
    fabric_measured: bool
    node_count: int
    nodes: List[ExecutorInfo]

    @property
    def free_count(self) -> int:
        return len(self.nodes)

    @property
    def gpu_type(self) -> str:
        return self.nodes[0].gpu_type if self.nodes else ""

    @property
    def gpus_per_node(self) -> int:
        return self.nodes[0].gpu_count if self.nodes else 0

    @property
    def price_per_node_hour(self) -> float:
        """Hourly price of the cheapest free node (whole host)."""
        return min((n.price_per_hour for n in self.nodes), default=0.0)

    def cheapest(self, count: int) -> List[ExecutorInfo]:
        """The ``count`` cheapest free nodes, or ``ValueError`` when fewer are free."""
        if count < 1:
            raise ValueError("A cluster needs at least one node")
        if count > len(self.nodes):
            raise ValueError(
                f"Only {len(self.nodes)} of {self.node_count} nodes on fabric {self.fabric_id} are free; "
                f"{count} requested"
            )
        return sorted(self.nodes, key=lambda n: (n.price_per_hour, n.id))[:count]


@dataclass
class Cluster:
    """The pods of one multi-node rental, in node-rank order.

    Node 0 is the natural ``MASTER_ADDR`` for ``torchrun``; every member reaches every other
    over the private overlay (``10.42.0.<rank+1>``) that the cluster image raises on start.
    """

    id: str
    pods: List[PodInfo]

    def __post_init__(self) -> None:
        self.pods = sorted(self.pods, key=lambda p: (p.cluster_node_index is None, p.cluster_node_index or 0))

    @property
    def size(self) -> int:
        return len(self.pods)

    @property
    def master(self) -> Optional[PodInfo]:
        return self.pods[0] if self.pods else None

    @property
    def master_addr(self) -> Optional[str]:
        """Overlay address of node 0 — what ``torchrun --master_addr`` and NCCL's bootstrap want."""
        return self.master.cluster_overlay_ip if self.master else None

    @property
    def status(self) -> str:
        """``RUNNING`` only when every member is; otherwise the first member state that is not."""
        states = [p.status.upper() for p in self.pods]
        if not states:
            return "EMPTY"
        return "RUNNING" if all(s == "RUNNING" for s in states) else next(s for s in states if s != "RUNNING")

    @property
    def price_per_hour(self) -> float:
        return sum(p.executor.price_per_hour for p in self.pods if p.executor)

    def node_rank(self, pod: PodInfo) -> int:
        for rank, member in enumerate(self.pods):
            if member.id == pod.id:
                return rank
        raise ValueError(f"Pod {pod.name or pod.id} is not a member of cluster {self.id}")

    def hostfile(self, slots: Optional[int] = None) -> str:
        """An ``mpirun``/DeepSpeed hostfile: one ``<overlay ip> slots=<gpus>`` line per node."""
        lines = []
        for pod in self.pods:
            n = slots if slots is not None else (pod.executor.gpu_count if pod.executor else 1)
            lines.append(f"{pod.cluster_overlay_ip or pod.host} slots={n}")
        return "\n".join(lines) + "\n"

    def torchrun_args(self, pod: PodInfo, *, master_port: int = 29500) -> str:
        """The ``torchrun`` rendezvous flags for ``pod``: ``--nnodes N --node_rank R --master_addr A --master_port P``."""
        return (
            f"--nnodes {self.size} --node_rank {self.node_rank(pod)} "
            f"--master_addr {self.master_addr} --master_port {master_port}"
        )


@dataclass
class Template(_Serializable):
    """Template information."""

    id: str
    name: str
    huid: str
    docker_image: str
    docker_image_tag: str
    category: str
    status: str


@dataclass
class BackupConfig(_Serializable):
    """Backup configuration information."""

    id: str
    huid: str
    pod_executor_id: str
    backup_frequency_hours: int
    retention_days: int
    backup_path: str
    is_active: bool
    created_at: str
    updated_at: Optional[str] = None


@dataclass
class BackupLog(_Serializable):
    """Backup log information."""

    id: str
    huid: str
    backup_config_id: str
    status: str
    started_at: str
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    progress: Optional[float] = None
    backup_volume_id: Optional[str] = None
    created_at: Optional[str] = None
    stage: Optional[str] = None
    total_files: Optional[int] = None
    processed_files: Optional[int] = None
    total_bytes: Optional[int] = None
    processed_bytes: Optional[int] = None
    deletion_state: Optional[str] = None
    physical_cleanup_at: Optional[str] = None
    status_message: Optional[str] = None
    elapsed_seconds: Optional[int] = None
    throughput_bytes_per_second: Optional[int] = None
    estimated_remaining_seconds: Optional[int] = None


@dataclass
class RestoreLog(_Serializable):
    """Restore log information."""

    id: str
    huid: str
    backup_id: str
    pod_id: str
    status: str
    progress: float
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    error_message: Optional[str] = None
    logs: Optional[List[str]] = None
    restore_path: Optional[str] = None
    backup_engine: Optional[str] = None
    restore_mode: Optional[str] = None
    stage: Optional[str] = None
    last_heartbeat_at: Optional[str] = None
    total_files: Optional[int] = None
    processed_files: Optional[int] = None
    total_bytes: Optional[int] = None
    processed_bytes: Optional[int] = None
    elapsed_seconds: Optional[int] = None
    throughput_bytes_per_second: Optional[int] = None
    estimated_remaining_seconds: Optional[int] = None


@dataclass
class SSHKey(_Serializable):
    """Public SSH key registered for the current user."""

    id: str
    name: str
    public_key: str
    created_at: Optional[str] = None


@dataclass
class VolumeInfo(_Serializable):
    """Volume information."""

    id: str
    huid: str
    name: str
    description: str
    created_at: str
    updated_at: Optional[str] = None
    current_size_bytes: int = 0
    current_file_count: int = 0
    current_size_gb: float = 0.0
    current_size_mb: float = 0.0
    last_metrics_update: Optional[str] = None


@dataclass
class GpuStats(_Serializable):
    """One GPU's utilisation as reported by ``nvidia-smi`` on the pod."""

    index: int
    name: str
    utilization_pct: Optional[float]
    memory_used_mib: Optional[float]
    memory_total_mib: Optional[float]
    temperature_c: Optional[float]
    power_draw_w: Optional[float]

    @property
    def memory_pct(self) -> Optional[float]:
        if not self.memory_total_mib or self.memory_used_mib is None:
            return None
        return round(100.0 * self.memory_used_mib / self.memory_total_mib, 1)

    def _derived(self) -> Dict[str, Any]:
        return {"memory_pct": self.memory_pct}


@dataclass
class WorkspaceInfo:
    """A workspace (a team with roles and a billing owner) as the API describes it."""

    id: str
    name: str
    role: str  # the caller's role: owner / admin / member
    billing_owner_user_id: str
    pending_billing_owner_user_id: Optional[str] = None
    created_at: Optional[str] = None
    # Only GET /users/me says so; None when read from GET /workspaces
    is_personal: Optional[bool] = None

    def matches(self, name_or_id: str) -> bool:
        """Whether ``name_or_id`` names this workspace: its id, or its name (case-insensitive)."""
        return name_or_id == self.id or name_or_id.lower() == self.name.lower()


@dataclass
class WorkspaceMember:
    user_id: str
    name: str
    email: Optional[str]
    role: str
    is_billing_owner: bool
    joined_at: Optional[str] = None


@dataclass
class ApiKeyScope:
    """One row of ``GET /keys/scopes``: what a scope lets a key do, in the server's words.

    ``description`` is the one sentence next to the picker's checkbox, ``can`` the "what this key can do"
    lines, ``route_families`` the routes it opens, ``default`` whether a key made without naming scopes gets it.
    """

    scope: str
    description: str
    title: str = ""
    can: List[str] = field(default_factory=list)
    route_families: List[str] = field(default_factory=list)
    default: bool = False


@dataclass
class ApiKeyInfo:
    """An API key row as ``GET /keys`` / ``POST /keys`` describe it.

    The budget fields are USD, one per window — ``daily_budget_usd`` (a UTC day), ``monthly_budget_usd`` (a
    UTC calendar month), ``max_budget_usd`` (the key's lifetime); ``spent_today_usd`` / ``spent_month_usd`` /
    ``spent_total_usd`` are what the key's pods were billed in each; a budget is ``None`` when the key has none. ``pod_visibility`` is ``own``
    (the key lists only the pods it rented) or ``account`` (every pod of the account); ``None`` from a server
    before per-key budgets. ``key`` is the secret when the server sent it (``POST /keys`` always; the list rows on servers
    that echo it): kept out of ``repr`` and of :meth:`to_dict`, so it is printed only where ``create`` prints it
    once. ``raw`` is the server's row, for fields this class does not name.
    """

    id: str
    name: str
    scopes: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    last_used: Optional[str] = None
    workspace_id: Optional[str] = None
    daily_budget_usd: Optional[float] = None
    monthly_budget_usd: Optional[float] = None
    max_budget_usd: Optional[float] = None
    spent_today_usd: Optional[float] = None
    spent_month_usd: Optional[float] = None
    spent_total_usd: Optional[float] = None
    pod_visibility: Optional[str] = None
    # active pods the key created, as the server counts them; None from a server before per-key budgets
    pods_count: Optional[int] = None
    key: Optional[str] = field(default=None, repr=False)
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)

    def matches(self, name_or_id: str) -> bool:
        """Whether ``name_or_id`` names this key: its id, or its name (case-insensitive)."""
        return name_or_id == self.id or name_or_id.lower() == self.name.lower()

    def to_dict(self) -> Dict[str, Any]:
        """The server's row without the key material, plus the normalised fields — what ``--json`` prints."""
        data = {k: v for k, v in self.raw.items() if k != "key"}
        for name in (
            "id", "name", "scopes", "created_at", "last_used", "workspace_id", "daily_budget_usd",
            "monthly_budget_usd", "max_budget_usd", "spent_today_usd", "spent_month_usd", "spent_total_usd",
            "pod_visibility", "pods_count",
        ):
            data[name] = getattr(self, name)
        return data


@dataclass
class ApiKeyRefusal:
    """One row of ``GET /keys/{id}/refusals`` (server support pending): a request the key's budget
    refused — when, which window was hit (``daily`` / ``monthly`` / ``max``), the route asked, the USD asked
    for, and the budget and spend at the time. ``raw`` is the server's row."""

    at: Optional[str] = None
    window: Optional[str] = None
    route: Optional[str] = None
    amount_usd: Optional[float] = None
    budget_usd: Optional[float] = None
    spent_usd: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False)


__all__ = [
    "ExecutorInfo",
    "PodInfo",
    "Template",
    "BackupConfig",
    "BackupLog",
    "RestoreLog",
    "VolumeInfo",
    "SSHKey",
    "GpuStats",
    "WorkspaceInfo",
    "WorkspaceMember",
    "ApiKeyScope",
    "ApiKeyInfo",
    "ApiKeyRefusal",
]
