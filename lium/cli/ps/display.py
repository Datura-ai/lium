"""Display formatting logic for ps command."""

from datetime import datetime, timezone
from typing import List, Optional
from rich.table import Table

from lium.sdk import PodInfo
from lium.cli.utils import console


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

    duration = datetime.now(timezone.utc) - dt_created
    hours = duration.total_seconds() / 3600

    if hours < 1:
        mins = duration.total_seconds() / 60
        return f"{mins:.0f}m"
    elif hours < 24:
        return f"{hours:.1f}h"
    else:
        days = hours / 24
        return f"{days:.1f}d"


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


def format_header(pod_count: int) -> str:
    """Format header text for pods list."""
    return f"Pods  ({pod_count} active)"


def build_pods_table(pods: List[PodInfo], short: bool = False) -> tuple[Table | None, str]:
    """Build pods table, returns (table, header)."""

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

    # Add columns
    table.add_column("Pod", justify="left", ratio=3, min_width=18, overflow="fold")
    table.add_column("Status", justify="left", width=11, no_wrap=True)
    table.add_column("Config", justify="left", width=12, no_wrap=True)
    table.add_column("Template", justify="left", ratio=2, min_width=12, overflow="ellipsis")
    table.add_column("$/h", justify="right", width=6, no_wrap=True)
    table.add_column("Spent", justify="right", width=8, no_wrap=True)
    table.add_column("Uptime", justify="right", width=7, no_wrap=True)
    if not short:
        table.add_column("Ports", justify="left", ratio=3, min_width=15, overflow="fold")
    table.add_column("Name", justify="left", ratio=2, min_width=15, overflow="fold")

    # Add rows
    for pod in pods:
        executor = pod.executor
        if executor:
            config = f"{pod.gpu_count}×{executor.gpu_type}" if pod.gpu_count > 1 else executor.gpu_type
            price_str = f"${pod.price:.2f}"
        else:
            config = "—"
            price_str = "—"

        status_color = console.pod_status_color(pod.status)
        status_text = f"[{status_color}]{pod.status.upper()}[/]"

        template_name = _format_template_name(pod.template)
        ports_display = f"{executor.ip if executor else ''}\n" + _format_ports(pod.ports)

        row = [
            console.get_styled(pod.huid, 'pod_id'),
            status_text,
            config,
            console.get_styled(template_name, 'info'),
            price_str,
            _format_cost(pod.created_at, pod.price),
            _format_uptime(pod.created_at),
        ]

        if not short:
            row.append(console.get_styled(ports_display, 'info'))

        row.append(console.get_styled(pod.name or "—", 'info'))

        table.add_row(*row)

    header = format_header(len(pods))
    return table, header
