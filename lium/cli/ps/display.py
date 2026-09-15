"""Display formatting logic for ps command."""

from datetime import datetime, timezone
from typing import List, Optional
from rich.table import Table

from lium.sdk import PodInfo, pod_ssh_command
from lium.cli.utils import console, pod_gpu_count


def _parse_timestamp(timestamp: str) -> Optional[datetime]:
    """Parse ISO format timestamp."""
    try:
        if timestamp.endswith('Z'):
            return datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        elif '+' not in timestamp and '-' not in timestamp[10:]:
            return datetime.fromisoformat(timestamp).replace(tzinfo=timezone.utc)
        else:
            return datetime.fromisoformat(timestamp)
    except (ValueError, AttributeError):
        return None


def _format_uptime(created_at: str) -> str:
    """Format uptime from created_at timestamp."""
    if not created_at:
        return "—"

    dt_created = _parse_timestamp(created_at)
    if not dt_created:
        return "—"

    return format_duration((datetime.now(timezone.utc) - dt_created).total_seconds())


def format_duration(seconds: float) -> str:
    """``32m`` / ``1.5h`` / ``1.2d`` — the uptime spelling every command shares (ps, describe, rm)."""
    hours = seconds / 3600
    if hours < 1:
        return f"{seconds / 60:.0f}m"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _format_cost(created_at: str, price_per_hour: Optional[float]) -> str:
    """Calculate and format cost based on uptime."""
    if not created_at or price_per_hour is None:
        return "—"

    dt_created = _parse_timestamp(created_at)
    if not dt_created:
        return "—"

    duration = datetime.now(timezone.utc) - dt_created
    hours = duration.total_seconds() / 3600
    cost = hours * price_per_hour
    return f"${cost:.2f}"


def _spend_cap_usd(created_at: str, removal_scheduled_at: Optional[str], price_per_hour: Optional[float]) -> Optional[float]:
    """What the pod will have cost when its scheduled removal fires; None without a schedule."""
    if not created_at or not removal_scheduled_at or price_per_hour is None:
        return None
    dt_created = _parse_timestamp(created_at)
    dt_removal = _parse_timestamp(removal_scheduled_at)
    if not dt_created or not dt_removal or dt_removal <= dt_created:
        return None
    return round((dt_removal - dt_created).total_seconds() / 3600 * price_per_hour, 2)


def _format_spent(created_at: str, removal_scheduled_at: Optional[str], price_per_hour: Optional[float]) -> str:
    """'$3.20' — or '$3.20/$12.50' when a removal is scheduled, spent against the cap."""
    spent = _format_cost(created_at, price_per_hour)
    cap = _spend_cap_usd(created_at, removal_scheduled_at, price_per_hour)
    return f"{spent}/${cap:.2f}" if cap is not None and spent != "—" else spent


def _format_template_name(template: dict) -> str:
    """Format template name for display."""
    if not template:
        return "—"

    name = template.get("name") or template.get("template_name") or "—"
    return name


def _format_ports(ports: dict) -> str:
    """Format port mappings."""
    if not ports:
        return "—"

    port_pairs = [f"{k}:{v}" for k, v in ports.items()]
    return ", ".join(port_pairs)


def _gpu_config(pod: PodInfo) -> Optional[str]:
    """``2×H100`` for a multi-GPU pod, ``H100`` for one; None without an executor.

    The count is the pod's own (``PodInfo.gpu_count``), not the host's: a GPU-split
    rental of 1 GPU on a 3×RTX 3090 node reads ``RTX3090``.
    """
    if not pod.executor:
        return None
    count = pod_gpu_count(pod)
    return f"{count}×{pod.executor.gpu_type}" if count and count > 1 else pod.executor.gpu_type


def compact_pod(pod: PodInfo, index: Optional[int] = None) -> dict:
    """Slim, table-equivalent JSON view of a pod.

    ``index`` is the 1-based row number in this listing — the number `lium rm 1`
    refers to, sorted or filtered as shown; None for a single-pod lookup
    (`ps <pod>`), which defines no rows.
    """
    executor = pod.executor
    view = {
        "index": index,
        "id": pod.id,
        "huid": pod.huid,
        "name": pod.name,
        "status": pod.status.upper() if pod.status else None,
        "gpu_type": executor.gpu_type if executor else None,
        "gpu_count": pod_gpu_count(pod),
        "config": _gpu_config(pod),
        "template": _format_template_name(pod.template) if pod.template else None,
        "price_per_hour": executor.price_per_hour if executor else None,
        "spent_usd": _spent_usd(pod.created_at, executor.price_per_hour if executor else None),
        "spend_cap_usd": _spend_cap_usd(
            pod.created_at, pod.removal_scheduled_at, executor.price_per_hour if executor else None
        ),
        "uptime": _format_uptime(pod.created_at),
        "created_at": pod.created_at,
        "ip": executor.ip if executor else None,
        "ports": pod.ports or {},
        "ssh_cmd": pod.ssh_cmd,
        "ssh_command": pod_ssh_command(pod),
        "removal_scheduled_at": pod.removal_scheduled_at,
        "jupyter_url": pod.jupyter_url,
    }
    workspace_id = getattr(pod, "workspace_id", None)  # a pod-shaped stub without the field is a pod outside any workspace
    if workspace_id is not None:
        # only when the server has workspaces: a server without them keeps today's JSON exactly
        view["workspace_id"] = workspace_id
    return view


def _spent_usd(created_at: str, price_per_hour: Optional[float]) -> Optional[float]:
    """Numeric counterpart of _format_cost; None when unknown."""
    if not created_at or price_per_hour is None:
        return None
    dt_created = _parse_timestamp(created_at)
    if not dt_created:
        return None
    hours = (datetime.now(timezone.utc) - dt_created).total_seconds() / 3600
    return round(hours * price_per_hour, 2)


def format_header(pod_count: int) -> str:
    """Format header text for pods list."""
    return f"Pods  ({pod_count} active)"


def build_pods_table(pods: List[PodInfo], short: bool = False, show_index: bool = True) -> tuple[Table | None, str]:
    """Build pods table, returns (table, header).

    ``show_index`` adds the ``#`` column: the row number other commands accept
    as a pod index. It is off for a single-pod lookup (`ps <pod>`), which
    defines no rows.
    """

    if not pods:
        return None, ""

    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )

    # the Spent cell is "$3.20" or, for a pod with a scheduled removal, "$200.00/$480.00" (15 chars) — spent against
    # the cap (DAH-2565). The column grows only when a listed pod has a cap, so a narrow terminal keeps its Ports.
    any_cap = any(
        pod.executor is not None
        and _spend_cap_usd(pod.created_at, pod.removal_scheduled_at, pod.executor.price_per_hour) is not None
        for pod in pods
    )
    spent_width = 15 if any_cap else 8

    # Add columns
    if show_index:
        table.add_column("#", justify="right", width=3, no_wrap=True)
    table.add_column("Pod", justify="left", ratio=3, min_width=18, overflow="fold")
    table.add_column("Status", justify="left", width=11, no_wrap=True)
    table.add_column("Config", justify="left", width=12, no_wrap=True)
    table.add_column("Template", justify="left", ratio=2, min_width=12, overflow="ellipsis")
    table.add_column("$/h", justify="right", width=6, no_wrap=True)
    table.add_column("Spent", justify="right", width=spent_width, no_wrap=True)
    table.add_column("Uptime", justify="right", width=7, no_wrap=True)
    if not short:
        table.add_column("Ports", justify="left", ratio=3, min_width=15, overflow="fold")
    table.add_column("Name", justify="left", ratio=2, min_width=15, overflow="fold")

    # Add rows
    for position, pod in enumerate(pods, start=1):
        executor = pod.executor
        if executor:
            config = _gpu_config(pod)
            price_str = f"${executor.price_per_hour:.2f}"
            price_per_hour = executor.price_per_hour
        else:
            config = "—"
            price_str = "—"
            price_per_hour = None

        status_color = console.pod_status_color(pod.status)
        status_text = f"[{status_color}]{pod.status.upper()}[/]"

        template_name = _format_template_name(pod.template)
        ports_display = f"{executor.ip if executor else ''}\n" + _format_ports(pod.ports)

        row = []
        if show_index:
            row.append(console.get_styled(str(position), 'dim'))
        row += [
            console.get_styled(pod.huid, 'pod_id'),
            status_text,
            config,
            console.get_styled(template_name, 'info'),
            price_str,
            _format_spent(pod.created_at, pod.removal_scheduled_at, price_per_hour),
            _format_uptime(pod.created_at),
        ]

        if not short:
            row.append(console.get_styled(ports_display, 'info'))

        row.append(console.get_styled(pod.name or "—", 'info'))

        table.add_row(*row)

    header = format_header(len(pods))
    return table, header
