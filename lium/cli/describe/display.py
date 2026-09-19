"""Manifest assembly and rendering for the describe command."""

from datetime import datetime, timezone
from typing import Optional
from rich.table import Table

from lium.sdk import ExecutorInfo, PodInfo, pod_ssh_command
from lium.cli.utils import console, pod_gpu_count
# Reused rather than reimplemented: the same timestamp handling and cost rounding
# `lium ps` already applies, so describe and ps never disagree on spend.
from lium.cli.ps.display import _parse_timestamp, _spent_usd

SSH_INTERNAL_PORT = "22"


def _uptime_hours(created_at: str) -> Optional[float]:
    """Hours since pod creation; None when the timestamp is unusable."""
    dt_created = _parse_timestamp(created_at) if created_at else None
    if not dt_created:
        return None
    return round((datetime.now(timezone.utc) - dt_created).total_seconds() / 3600, 2)


def _port_number(port: str | int) -> int | str:
    """Port as a number when it is one; left untouched otherwise (e.g. "22/tcp")."""
    try:
        return int(port)
    except (TypeError, ValueError):
        return port


def _ports_section(ports: dict) -> dict:
    """Port mapping split into the SSH port and the ports left for services.

    Keys of the mapping are ports *inside* the container, values are the ports
    reachable from outside. Getting that direction wrong is the single most
    common way an agent loses time on a pod, so the manifest states it.

    Keys are compared as strings: the backend sends them as JSON keys today, but
    a mapping built in Python carries real ints, and neither may drop the SSH port.
    """
    mapping = ports or {}
    service_ports = [
        {"internal": _port_number(internal), "external": external}
        for internal, external in mapping.items()
        if str(internal) != SSH_INTERNAL_PORT
    ]
    ssh_external = next(
        (external for internal, external in mapping.items() if str(internal) == SSH_INTERNAL_PORT),
        None,
    )
    return {
        "mapping": mapping,
        "direction": "internal -> external",
        "ssh_external": ssh_external,
        "service_ports": service_ports,
    }


def event_view(event: Optional[dict]) -> Optional[dict]:
    """One pod event (GET /pods/{id}/events, or last_event on the detail) with the keys that say
    what happened: when, which status it went to, why, and the failure text if any."""
    if not event:
        return None
    return {
        "at": event.get("created_at"),
        "type": event.get("sub_event_type") or event.get("event_type"),
        "from_status": event.get("from_status"),
        "to_status": event.get("to_status"),
        "reason": event.get("reason"),
        "detail": event.get("detail") or event.get("error"),
    }


def format_event(view: Optional[dict]) -> str:
    """`DELETED (scheduled_termination) — ttl · 2026-09-05T23:36:32`; `—` when there is none."""
    if not view:
        return "—"
    head = view["to_status"] or view["type"] or "event"
    if view["reason"]:
        head = f"{head} ({view['reason']})"
    if view["detail"]:
        head = f"{head} — {view['detail']}"
    return f"{head} · {view['at']}" if view["at"] else head


def disk_health_view(executor: Optional[ExecutorInfo], detail: Optional[dict]) -> Optional[dict]:
    """The node's disk verdict (backend, DAH-2929) and the readings behind it (validator probe,
    DAH-2928). None when the node's validator has not probed it."""
    specs = (executor.specs if executor else None) or {}
    health = specs.get("disk_health")
    verdict = ((detail or {}).get("executor") or {}).get("disk_health_ok")
    if not isinstance(health, dict) and verdict is None:
        return None
    health = health if isinstance(health, dict) else {}
    return {
        "ok": verdict,
        "read_only_mounts": health.get("read_only_mounts") or [],
        "write_probe": health.get("write_probe"),
        "kernel_io_errors": health.get("kernel_io_errors"),
        "kernel_io_error_lines": health.get("kernel_io_error_lines") or [],
        "block_io_errors": health.get("block_io_errors") or {},
        "nvme_states": health.get("nvme_states") or {},
        "smart": health.get("smart"),
    }


def format_disk_health(view: Optional[dict]) -> str:
    """`ok`, `unknown (not probed)`, or the readings that are wrong, one phrase each."""
    if view is None:
        return "unknown (not probed)"
    problems = []
    if view["read_only_mounts"]:
        problems.append(f"read-only: {', '.join(view['read_only_mounts'])}")
    if view["write_probe"] == "failed":
        problems.append("write probe failed")
    if view["kernel_io_errors"]:
        problems.append(f"{view['kernel_io_errors']} kernel I/O errors")
    if view["block_io_errors"]:
        problems.append("block errors: " + ", ".join(f"{d}={n}" for d, n in view["block_io_errors"].items()))
    if view["nvme_states"]:
        problems.append("nvme: " + ", ".join(f"{d} {s}" for d, s in view["nvme_states"].items()))
    if isinstance(view["smart"], dict):
        failed = [d for d, verdict in view["smart"].items() if verdict != "PASSED"]
        if failed:
            problems.append("SMART: " + ", ".join(failed))
    if problems:
        return "PROBLEM — " + "; ".join(problems)
    return "ok" if view["ok"] is not False else "PROBLEM (see --json)"


def latest_lifecycle_event(events: list[dict]) -> Optional[dict]:
    """The newest `pod-lifecycle` event — the one whose to_status and reason say what happened to the pod — or,
    when the log has none, the last event of any kind. A normal delete ends with `pod-delete.success`, which
    carries neither, so the headline would lose the cause if it took the last row."""
    for event in reversed(events):
        if event.get("event_type") == "pod-lifecycle" or str(event.get("sub_event_type") or "").startswith("pod-lifecycle"):
            return event
    return events[-1] if events else None


def build_gone_manifest(pod_id: str, events: list[dict]) -> dict:
    """What is left of a pod that is no longer listed: its id and the events the backend kept."""
    views = [event_view(event) for event in events]
    return {
        "pod": {"id": pod_id, "huid": None, "name": next((e.get("pod_name") for e in reversed(events) if e.get("pod_name")), None),
                "status": "GONE", "created_at": None, "uptime_hours": None},
        "last_event": event_view(latest_lifecycle_event(events)),
        "events": views,
    }


def build_manifest(pod: PodInfo, detail: Optional[dict] = None) -> dict:
    """Everything an agent needs to work on this pod, as one JSON document.

    ``detail`` is GET /pods/{id} when the caller fetched it: it carries the pod's last lifecycle
    event and the node's disk verdict, which the listing does not.
    """
    executor = pod.executor
    template = pod.template or {}
    price_per_hour = executor.price_per_hour if executor else None
    detail = detail or {}

    return {
        "pod": {
            "id": pod.id,
            "huid": pod.huid,
            "name": pod.name,
            "status": pod.status.upper() if pod.status else None,
            "created_at": pod.created_at,
            "uptime_hours": _uptime_hours(pod.created_at),
        },
        "gpu": {
            "type": executor.gpu_type,
            "count": pod_gpu_count(pod),
            "model": executor.gpu_model or None,
            "driver_version": executor.driver_version or None,
            "max_cuda_version": executor.max_cuda_version,
            # How the GPUs are wired to each other, as the node's validator saw it with
            # `nvidia-smi topo -m`. `link` is the one-word answer for a TP/FSDP job; `interconnect`
            # carries the counts and the GPU x GPU matrix. None until the node reports it.
            "link": executor.link,
            "nvlink": executor.nvlink,
            "p2p": executor.p2p,
            "interconnect": executor.interconnect,
        } if executor else None,
        "machine": {
            "executor_id": executor.id,
            "ip": executor.ip,
            "location": executor.location,
            "tier": executor.tier,
            "docker_in_docker": executor.docker_in_docker,
            "download_mbps": executor.effective_download_speed_mbps,
            "upload_mbps": executor.effective_upload_speed_mbps,
        } if executor else None,
        "ports": _ports_section(pod.ports),
        "access": {
            "ssh_cmd": pod.ssh_cmd,
            "ssh_command": pod_ssh_command(pod),
            "jupyter_url": pod.jupyter_url,
        },
        "template": {
            "name": template.get("name") or template.get("template_name"),
            "docker_image": template.get("docker_image"),
            "docker_image_tag": template.get("docker_image_tag"),
        } if template else None,
        "storage": {
            "volume_encryption_enabled": pod.enable_volume_encryption,
            "volume_encryption_status": pod.volume_encryption_status,
        },
        "billing": {
            "price_per_hour": price_per_hour,
            "spent_usd": _spent_usd(pod.created_at, price_per_hour),
            "removal_scheduled_at": pod.removal_scheduled_at,
        },
        # why the pod is REBOOT_FAILED / BROKEN / DELETING, from the backend's lifecycle record
        "last_event": event_view(detail.get("last_event")),
        "node_disk": disk_health_view(executor, detail),
    }


def _format_ports(ports_section: dict) -> str:
    """One line per mapped port, SSH first."""
    mapping = ports_section["mapping"]
    if not mapping:
        return "—"

    ssh_external = ports_section["ssh_external"]
    lines = []
    if ssh_external:
        lines.append(f"{ssh_external} → 22 (ssh)")
    lines.extend(
        f"{service['external']} → {service['internal']}"
        for service in ports_section["service_ports"]
    )
    return "\n".join(lines)


def _format_link(gpu: dict) -> str:
    """One line a renter can act on: the link class, the P2P verdict, the pair counts."""
    if gpu.get("link") is None:
        return "— (not reported by the node's validator yet; run nvidia-smi topo -m on the pod)"
    interconnect = gpu.get("interconnect") or {}
    parts = ["NVLink" if gpu["nvlink"] else "PCIe"]
    if gpu["nvlink"] and interconnect.get("nvlink_links"):
        parts[0] += f" ×{interconnect['nvlink_links']}"
    if not gpu["nvlink"] and interconnect.get("pcie_class"):
        parts[0] += f" ({interconnect['pcie_class']})"
    if interconnect.get("gpu_pairs"):
        parts.append(f"{interconnect.get('nvlink_pairs') or 0}/{interconnect['gpu_pairs']} pairs on NVLink")
    if gpu.get("p2p") is True:
        parts.append("P2P ok")
    elif gpu.get("p2p") is False:
        parts.append("no P2P (NCCL needs NCCL_P2P_DISABLE=1)")
    return ", ".join(parts)


def _format_topology(interconnect: dict | None) -> str | None:
    """The GPU x GPU matrix as `nvidia-smi topo -m` prints it, one row per GPU; None when absent."""
    matrix = (interconnect or {}).get("matrix")
    if not matrix or not isinstance(matrix, list):
        return None
    width = max((len(str(cell)) for row in matrix for cell in row), default=1)
    return "\n".join(
        f"GPU{index} " + " ".join(f"{str(cell):>{width}}" for cell in row) for index, row in enumerate(matrix)
    )


def _format_net(machine: dict) -> str:
    """The speed-test figures in Mbps, ``↓300 ↑480 Mbps (speed test)``; a dash for a missing one."""
    down, up = machine.get("download_mbps"), machine.get("upload_mbps")
    return f"↓{int(down) if down else '—'} ↑{int(up) if up else '—'} Mbps (speed test)"


def build_manifest_table(manifest: dict) -> Table:
    """Human-readable rendering of the same manifest the --json flag emits."""
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 1))
    table.add_column(justify="left", style="dim", no_wrap=True)
    table.add_column(justify="left", overflow="fold")

    pod = manifest["pod"]
    gpu = manifest["gpu"]
    machine = manifest["machine"]
    template = manifest["template"]
    billing = manifest["billing"]

    table.add_row("Pod", console.get_styled(pod["huid"] or "—", "pod_id"))
    table.add_row("Name", pod["name"] or "—")
    table.add_row("Status", f"[{console.pod_status_color(pod['status'] or '')}]{pod['status'] or '—'}[/]")
    table.add_row("Uptime", f"{pod['uptime_hours']}h" if pod["uptime_hours"] is not None else "—")

    if gpu:
        config = f"{gpu['count']}×{gpu['type']}" if (gpu["count"] or 0) > 1 else (gpu["type"] or "—")
        table.add_row("GPU", config)
        table.add_row("Driver", gpu["driver_version"] or "—")
        table.add_row("Max CUDA", str(gpu["max_cuda_version"]) if gpu["max_cuda_version"] else "—")
        if (gpu["count"] or 0) > 1 or gpu.get("link") is not None:
            table.add_row("Link", _format_link(gpu))
            topology = _format_topology(gpu.get("interconnect"))
            if topology:
                table.add_row("Topology", topology)
    if machine:
        table.add_row("IP", machine["ip"] or "—")
        table.add_row("Tier", machine["tier"] or "—")
        table.add_row("Net", _format_net(machine))
    if template:
        table.add_row("Template", template["name"] or "—")
        table.add_row("Image", template["docker_image"] or "—")

    table.add_row("Ports ext→int", _format_ports(manifest["ports"]))
    table.add_row("SSH", manifest["access"]["ssh_command"] or "—")

    price = billing["price_per_hour"]
    spent = billing["spent_usd"]
    table.add_row("$/h", f"${price:.2f}" if price is not None else "—")
    table.add_row("Spent", f"${spent:.2f}" if spent is not None else "—")
    if billing["removal_scheduled_at"]:
        table.add_row("Removal at", billing["removal_scheduled_at"])

    if manifest.get("last_event"):
        table.add_row("Last event", format_event(manifest["last_event"]))
    if machine:
        table.add_row("Node disk", format_disk_health(manifest.get("node_disk")))

    return table


def build_events_table(manifest: dict) -> Table:
    """The event log of a pod that is no longer listed, oldest first."""
    table = Table(show_header=True, header_style="dim", box=None, pad_edge=False, padding=(0, 1))
    table.add_column("When", no_wrap=True)
    table.add_column("Event", no_wrap=True)
    table.add_column("What", overflow="fold")
    for view in manifest["events"]:
        what = format_event({**view, "at": None})
        table.add_row(view["at"] or "—", view["type"] or "—", what)
    return table
