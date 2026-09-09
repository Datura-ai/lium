"""Clusters subcommands: list, up, ps, show, rm."""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import click

from lium.cli import ui
from lium.cli.up.parsing import parse_duration
from lium.cli.utils import (
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    CliFailure,
    ensure_config,
    handle_errors,
)
from lium.sdk import Cluster, ClusterOffer, Lium, LiumError, LiumNotFoundError, PodStartError

from .display import (
    build_clusters_table,
    build_members_table,
    build_offers_table,
    cluster_to_dict,
    offer_to_dict,
)

FORMAT_OPTION = click.option(
    "--format", "output_format", type=click.Choice(["table", "json"]), default="table", show_default=True,
    help="Output format. 'json' emits machine-readable JSON to stdout.",
)


# -- last listing, so `clusters up 1` can name a fabric by row ---------------------------------

def _selection_file():
    from lium.cli.settings import config

    return config.config_dir / "last_cluster_selection.json"


def store_cluster_selection(offers: List[ClusterOffer]) -> None:
    data = {"timestamp": datetime.now().isoformat(), "fabrics": [o.fabric_id for o in offers]}
    with open(_selection_file(), "w") as f:
        json.dump(data, f, indent=2)


def get_last_cluster_selection() -> Optional[Dict[str, Any]]:
    path = _selection_file()
    if not path.exists():
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def resolve_fabric(lium: Lium, fabric: str) -> ClusterOffer:
    """A fabric by row number from the last ``lium clusters``, by exact id, or by unique prefix."""
    if fabric.isdigit():
        last = get_last_cluster_selection() or {}
        fabrics = last.get("fabrics") or []
        index = int(fabric)
        if not fabrics:
            raise CliFailure("no_clusters_cached", "No fabrics cached. Run 'lium clusters' first.", EXIT_GENERAL_ERROR)
        if not 1 <= index <= len(fabrics):
            raise CliFailure(
                "invalid_arguments", f"Fabric index {index} out of range (1-{len(fabrics)}).", EXIT_CONFIGURATION_ERROR
            )
        fabric = fabrics[index - 1]
    offer = lium.cluster_offer(fabric)
    if offer is None:
        raise CliFailure("fabric_not_found", f"No rentable fabric matches '{fabric}'. Run 'lium clusters'.", EXIT_POD_NOT_FOUND)
    return offer


def resolve_cluster(lium: Lium, ref: str) -> Cluster:
    """A cluster by id (or unique prefix), or by the name its member pods share."""
    try:
        return lium.cluster(ref)
    except LiumNotFoundError:
        pass
    named = [c for c in lium.my_clusters() if c.master and c.master.name == ref]
    if len(named) == 1:
        return named[0]
    detail = f"{len(named)} clusters are named '{ref}'; use the cluster id." if named else f"No cluster '{ref}'. Run 'lium clusters ps'."
    raise CliFailure("cluster_not_found", detail, EXIT_POD_NOT_FOUND)


# -- list --------------------------------------------------------------------------------------

@click.command("list")
@FORMAT_OPTION
@handle_errors
def clusters_list_command(output_format: str):
    """Fabrics with free nodes that can be rented as one cluster."""
    ensure_config()
    lium = Lium()
    offers = ui.load("Loading clusters", lium.clusters) if output_format == "table" else lium.clusters()
    store_cluster_selection(offers)

    if output_format == "json":
        click.echo(json.dumps([offer_to_dict(o) for o in offers], indent=2, ensure_ascii=False))
        return
    if not offers:
        ui.info("No fabric has free nodes right now.")
        return
    table, header, tip = build_offers_table(offers)
    ui.info(header)
    ui.print(table)
    ui.print("")
    ui.info(tip)


# -- up ----------------------------------------------------------------------------------------

@click.command("up")
@click.argument("fabric")
@click.option("--nodes", "-N", "node_count", type=int, required=True, help="How many whole nodes to rent (>= 2)")
@click.option("--name", "-n", "name", required=True, help="Pod name given to every member")
@click.option("--template_id", "-t", "template_id", help="Template ID (default: the newest daturaai/lium-cluster template)")
@click.option("--ports", "-p", type=int, help="Ports to expose per node")
@click.option("--ttl", help="Auto-terminate every member after a duration (e.g. 6h, 45m, 2d)")
@click.option("--wait/--no-wait", default=True, show_default=True, help="Wait until every member is RUNNING with SSH")
@click.option("--timeout", type=int, default=900, show_default=True, help="Seconds to wait with --wait")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@FORMAT_OPTION
@handle_errors
def clusters_up_command(
    fabric: str, node_count: int, name: str, template_id: Optional[str], ports: Optional[int], ttl: Optional[str],
    wait: bool, timeout: int, yes: bool, output_format: str,
):
    """Rent N whole nodes of one fabric as a single all-or-nothing cluster.

    FABRIC is a row number from the last 'lium clusters', a fabric id, or a unique prefix of one.
    The cheapest free nodes are taken. Every member runs the cluster template, shares the pod
    name, and gets a private overlay address (10.42.0.<rank+1>); node 0 is the master.
    """
    ensure_config()
    if node_count < 2:
        raise CliFailure("invalid_arguments", "--nodes must be at least 2.", EXIT_CONFIGURATION_ERROR)
    ttl_delta = None
    if ttl:
        ttl_delta, error = parse_duration(ttl)
        if error:
            raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    lium = Lium()
    offer = resolve_fabric(lium, fabric)
    try:
        chosen = offer.cheapest(node_count)
    except ValueError as exc:
        raise CliFailure("not_enough_nodes", str(exc), EXIT_GENERAL_ERROR) from exc

    hourly = sum(n.price_per_hour for n in chosen)
    # money is spent only after -y or an answered prompt, in every output mode — as `lium up` does
    if not yes:
        summary = (
            f"Rent {node_count}×({offer.gpus_per_node}×{offer.gpu_type}) on {offer.fabric_type} fabric "
            f"{offer.fabric_id} as cluster '{name}' for ${hourly:.2f}/h?"
        )
        if not ui.confirm(summary, stderr=output_format == "json"):
            return

    def rent() -> Cluster:
        return lium.up_cluster([n.id for n in chosen], name=name, template_id=template_id, ports=ports, wait=False)

    cluster = ui.load(f"Renting {node_count}-node cluster", rent) if output_format == "table" else rent()

    # The TTL is scheduled before any waiting: a --wait timeout must not leave N
    # nodes billing with nothing scheduled.
    scheduled = None
    if ttl_delta is not None:
        scheduled = datetime.now(timezone.utc) + ttl_delta
        # Every member is tried: a failure on one must not leave the rest unscheduled, and the
        # error names the billing cluster and the members without a TTL (as `lium up` does).
        unscheduled: List[str] = []
        last_error: Optional[Exception] = None
        for pod in cluster.pods:
            try:
                lium.schedule_termination(pod, termination_time=scheduled.isoformat())
            except LiumError as exc:
                unscheduled.append(pod.name or pod.id)
                last_error = exc
        if unscheduled:
            warning = (
                f"Cluster {cluster.id} is rented and billing; auto-termination was NOT scheduled on "
                f"{', '.join(unscheduled)}: {last_error}. Remove with 'lium clusters rm {cluster.id[:8]}' "
                f"or schedule each member with 'lium rm <pod> --in {ttl}'."
            )
            if output_format == "json":
                data = cluster_to_dict(cluster)
                data.update(ok=False, removal_scheduled_at=None, unscheduled=unscheduled, error=warning)
                click.echo(json.dumps(data, indent=2, ensure_ascii=False))
                raise SystemExit(EXIT_API_ERROR)
            raise CliFailure("cluster_ttl_not_scheduled", warning, EXIT_API_ERROR) from last_error

    if wait:
        def ready() -> Cluster:
            return lium.wait_cluster_ready(cluster, timeout=timeout)

        try:
            cluster = ui.load(f"Waiting for {node_count} members", ready) if output_format == "table" else ready()
        except (TimeoutError, PodStartError) as exc:
            billing = (
                f"every member terminates at {scheduled.strftime('%Y-%m-%d %H:%M UTC')}" if scheduled
                else "the members are rented and billing"
            )
            code = "cluster_member_failed" if isinstance(exc, PodStartError) else "cluster_not_ready"
            raise CliFailure(code, f"{exc}. {billing[0].upper() + billing[1:]}; see 'lium clusters ps'.",
                             EXIT_API_ERROR) from exc

    if output_format == "json":
        data = cluster_to_dict(cluster)
        data["removal_scheduled_at"] = scheduled.isoformat() if scheduled else None
        click.echo(json.dumps(data, indent=2, ensure_ascii=False))
        return

    ui.success(f"Cluster {cluster.id} ({cluster.size} nodes, ${cluster.price_per_hour:.2f}/h) {cluster.status}")
    ui.print(build_members_table(cluster))
    ui.print("")
    ui.info(f"MASTER_ADDR={cluster.master_addr}   hostfile: lium clusters show {cluster.id[:8]} --hostfile")
    if scheduled:
        ui.info(f"Every member terminates at {scheduled.strftime('%Y-%m-%d %H:%M UTC')}")


# -- ps ----------------------------------------------------------------------------------------

@click.command("ps")
@FORMAT_OPTION
@handle_errors
def clusters_ps_command(output_format: str):
    """Your cluster rentals."""
    ensure_config()
    lium = Lium()
    clusters = ui.load("Loading clusters", lium.my_clusters) if output_format == "table" else lium.my_clusters()
    if output_format == "json":
        click.echo(json.dumps([cluster_to_dict(c) for c in clusters], indent=2, ensure_ascii=False))
        return
    if not clusters:
        ui.info("No clusters. 'lium clusters' lists fabrics you can rent.")
        return
    table, header = build_clusters_table(clusters)
    ui.info(header)
    ui.print(table)


# -- show --------------------------------------------------------------------------------------

@click.command("show")
@click.argument("cluster")
@click.option("--hostfile", is_flag=True, help="Print only an mpirun/DeepSpeed hostfile (overlay IP + slots per node)")
@click.option("--torchrun", "torchrun_rank", type=int, default=None,
              help="Print only the torchrun rendezvous flags for the node with this rank")
@FORMAT_OPTION
@handle_errors
def clusters_show_command(cluster: str, hostfile: bool, torchrun_rank: Optional[int], output_format: str):
    """Members of a cluster: rank, overlay IP, SSH — everything a launcher needs.

    CLUSTER is a cluster id (or unique prefix) or the name its members share.
    """
    ensure_config()
    lium = Lium()
    found = resolve_cluster(lium, cluster)
    if hostfile:
        click.echo(found.hostfile(), nl=False)
        return
    if torchrun_rank is not None:
        member = next((p for p in found.pods if p.cluster_node_index == torchrun_rank), None)
        if member is None:
            raise CliFailure("invalid_arguments", f"Cluster {found.id} has no node with rank {torchrun_rank}.",
                             EXIT_CONFIGURATION_ERROR)
        click.echo(found.torchrun_args(member))
        return
    if output_format == "json":
        click.echo(json.dumps(cluster_to_dict(found), indent=2, ensure_ascii=False))
        return
    ui.info(f"Cluster {found.id}  ({found.size} nodes, {found.status}, ${found.price_per_hour:.2f}/h)")
    ui.print(build_members_table(found))
    ui.print("")
    ui.info(f"MASTER_ADDR={found.master_addr}   torchrun: lium clusters show {found.id[:8]} --torchrun <rank>")


# -- rm ----------------------------------------------------------------------------------------

@click.command("rm")
@click.argument("cluster")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@FORMAT_OPTION
@handle_errors
def clusters_rm_command(cluster: str, yes: bool, output_format: str):
    """Remove every member pod of a cluster."""
    ensure_config()
    lium = Lium()
    found = resolve_cluster(lium, cluster)
    if not yes:
        if not ui.confirm(f"Remove cluster {found.id} ({found.size} pods, ${found.price_per_hour:.2f}/h)?", stderr=output_format == "json"):
            return
    results = lium.rm_cluster(found)
    failed = [r for r in results if not r["success"]]
    warning = (
        f"{len(failed)} of {len(results)} members were not removed; they are still billing — "
        "retry or remove them with 'lium rm'."
    ) if failed else None
    if output_format == "json":
        click.echo(json.dumps({"ok": not failed, "cluster": found.id, "results": results, "error": warning}, indent=2))
        if failed:
            raise SystemExit(EXIT_API_ERROR)
        return
    for r in results:
        (ui.error if not r["success"] else ui.success)(
            f"{r['pod'][:8]} {'removed' if r['success'] else 'failed: ' + str(r['error'])}"
        )
    if failed:
        raise CliFailure("cluster_removal_failed", warning, EXIT_API_ERROR)
