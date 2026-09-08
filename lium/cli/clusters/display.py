"""Clusters display formatting."""

from typing import Any, Dict, List

from rich.table import Table
from rich.text import Text

from lium.cli import ui
from lium.cli.utils import mid_ellipsize
from lium.sdk import Cluster, ClusterOffer, PodInfo


def offer_to_dict(offer: ClusterOffer) -> Dict[str, Any]:
    return {
        "fabric_id": offer.fabric_id,
        "fabric_type": offer.fabric_type,
        "link_rate": offer.link_rate,
        "fabric_measured": offer.fabric_measured,
        "node_count": offer.node_count,
        "free_count": offer.free_count,
        "gpu_type": offer.gpu_type,
        "gpus_per_node": offer.gpus_per_node,
        "price_per_node_hour": offer.price_per_node_hour,
        "nodes": [
            {
                "id": n.id,
                "huid": n.huid,
                "gpu_type": n.gpu_type,
                "gpu_count": n.gpu_count,
                "price_per_hour": n.price_per_hour,
                "country": (n.location or {}).get("country"),
                "tier": n.tier,
            }
            for n in offer.nodes
        ],
    }


def pod_to_dict(pod: PodInfo) -> Dict[str, Any]:
    return {
        "id": pod.id,
        "huid": pod.huid,
        "name": pod.name,
        "status": pod.status,
        "node_rank": pod.cluster_node_index,
        "overlay_ip": pod.cluster_overlay_ip,
        "ssh_cmd": pod.ssh_cmd,
        "host": pod.host,
        "ssh_port": pod.ssh_port,
        "ports": pod.ports,
        "gpu_type": pod.executor.gpu_type if pod.executor else None,
        "gpu_count": pod.executor.gpu_count if pod.executor else None,
        "price_per_hour": pod.executor.price_per_hour if pod.executor else None,
    }


def cluster_to_dict(cluster: Cluster) -> Dict[str, Any]:
    return {
        "id": cluster.id,
        "name": cluster.master.name if cluster.master else None,
        "status": cluster.status,
        "size": cluster.size,
        "master_addr": cluster.master_addr,
        "price_per_hour": cluster.price_per_hour,
        "hostfile": cluster.hostfile(),
        "pods": [pod_to_dict(p) for p in cluster.pods],
    }


def _base_table() -> Table:
    return Table(show_header=True, header_style="dim", box=None, pad_edge=False, expand=True, padding=(0, 1))


def build_offers_table(offers: List[ClusterOffer]) -> tuple[Table, str, str]:
    header = f"{Text('Clusters', style='bold')}  ({len(offers)} fabric{'s' if len(offers) != 1 else ''} with free nodes)"
    tip = (
        f"Tip: {ui.styled('lium clusters up <#|fabric> --nodes N -n <name>', 'success')} "
        f"{ui.styled('# rent N whole nodes of one fabric as a cluster', 'dim')}"
    )
    table = _base_table()
    table.add_column("", justify="right", width=3, no_wrap=True, style="dim")
    table.add_column("Fabric", justify="left", ratio=3, min_width=18, overflow="ellipsis")
    table.add_column("Type", justify="left", width=10, no_wrap=True)
    table.add_column("Node", justify="left", width=14, no_wrap=True)
    table.add_column("Free/Total", justify="right", width=10, no_wrap=True)
    table.add_column("Link", justify="left", width=18, no_wrap=True)
    table.add_column("Measured", justify="center", width=8, no_wrap=True)
    table.add_column("$/node·h", justify="right", width=9, no_wrap=True)
    table.add_column("Loc", justify="left", width=5, no_wrap=True)

    for idx, offer in enumerate(offers, 1):
        countries = sorted({(n.location or {}).get("country") or "?" for n in offer.nodes})
        table.add_row(
            str(idx),
            ui.styled(mid_ellipsize(offer.fabric_id, 40), "id"),
            offer.fabric_type,
            f"{offer.gpus_per_node}×{offer.gpu_type}" if offer.nodes else "—",
            f"{offer.free_count}/{offer.node_count}",
            offer.link_rate or "—",
            ui.styled("yes", "success") if offer.fabric_measured else ui.styled("no", "warning"),
            f"${offer.price_per_node_hour:.2f}" if offer.nodes else "—",
            ",".join(countries) if countries else "—",
        )
    return table, header, tip


def build_clusters_table(clusters: List[Cluster]) -> tuple[Table, str]:
    header = f"{Text('Your clusters', style='bold')}  ({len(clusters)})"
    table = _base_table()
    table.add_column("", justify="right", width=3, no_wrap=True, style="dim")
    table.add_column("Cluster", justify="left", ratio=2, min_width=14, overflow="ellipsis")
    table.add_column("Name", justify="left", ratio=2, min_width=10, overflow="ellipsis")
    table.add_column("Status", justify="left", width=10, no_wrap=True)
    table.add_column("Nodes", justify="right", width=6, no_wrap=True)
    table.add_column("Node", justify="left", width=14, no_wrap=True)
    table.add_column("Master", justify="left", width=12, no_wrap=True)
    table.add_column("$/h", justify="right", width=8, no_wrap=True)

    for idx, cluster in enumerate(clusters, 1):
        first = cluster.master
        node = f"{first.executor.gpu_count}×{first.executor.gpu_type}" if first and first.executor else "—"
        table.add_row(
            str(idx),
            ui.styled(mid_ellipsize(cluster.id, 20), "id"),
            first.name if first else "—",
            ui.styled(cluster.status, "success" if cluster.status == "RUNNING" else "warning"),
            str(cluster.size),
            node,
            cluster.master_addr or "—",
            f"${cluster.price_per_hour:.2f}",
        )
    return table, header


def build_members_table(cluster: Cluster) -> Table:
    table = _base_table()
    table.add_column("Rank", justify="right", width=4, no_wrap=True)
    table.add_column("Pod", justify="left", ratio=2, min_width=14, overflow="ellipsis")
    table.add_column("Status", justify="left", width=10, no_wrap=True)
    table.add_column("Overlay IP", justify="left", width=12, no_wrap=True)
    table.add_column("GPUs", justify="left", width=12, no_wrap=True)
    table.add_column("SSH", justify="left", ratio=3, min_width=24, overflow="fold")
    for pod in cluster.pods:
        rank = pod.cluster_node_index
        table.add_row(
            str(rank) if rank is not None else "?",
            ui.styled(pod.huid, "id"),
            ui.styled(pod.status, "success" if pod.status.upper() == "RUNNING" else "warning"),
            pod.cluster_overlay_ip or "—",
            f"{pod.executor.gpu_count}×{pod.executor.gpu_type}" if pod.executor else "—",
            pod.ssh_cmd or ui.styled("(not ready)", "dim"),
        )
    return table
