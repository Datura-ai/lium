"""List (ls) command implementation."""

import json
from typing import Optional, List
import click
from rich.markup import escape

from lium.sdk import Lium, ExecutorInfo
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    console,
    handle_errors,
    resolve_output_format,
    store_executor_selection,
)
from lium.cli.completion import get_gpu_completions
from lium.cli.workspaces.context import show_workspace
from . import validation, display, filters as node_filters
from .actions import GetExecutorsAction


def ls_store_executor(gpu_type: Optional[str] = None) -> List[ExecutorInfo]:
    """Load and store nodes without displaying them, in the order `lium ls` prints them."""
    sorted_executors, _ = display.sort_executors(Lium().ls(gpu_type=gpu_type))
    store_executor_selection(sorted_executors)
    return sorted_executors


@click.command("ls", epilog="Use --format json for machine-readable output.")
@click.option("--gpu", "gpu_type", shell_complete=get_gpu_completions, help="Filter by GPU type, e.g. A100")
@click.option("--count", "gpu_count", type=int, help="Exact GPU count to match (e.g., 1, 8)")
@click.option("--min-cuda", "min_cuda_version", type=float, help="Minimum CUDA version, e.g. 12.4 (NVIDIA drivers are backward compatible)")
@click.option("--country", "countries", multiple=True, metavar="CODE|NAME", help="Only nodes in these countries (ISO code or name; repeatable or comma-separated)")
@click.option("--min-vram", "min_vram_gb", type=float, metavar="GB", help="Minimum memory per GPU in GB, e.g. 80")
@click.option("--max-price", "max_price", type=float, metavar="USD", help="Maximum price per GPU-hour, e.g. 2.50")
@click.option("--tier", type=click.Choice(["spot", "secure"]), help="Only spot (reclaimable) or secure nodes")
@click.option("--min-cpus", "min_cpus", type=int, help="Minimum CPU thread count, e.g. 32 (the CPUs column)")
@click.option(
    "--nvlink",
    is_flag=True,
    default=False,
    help="Only nodes whose GPUs are all joined by NVLink (Link column NV#). Nodes with no topology report yet are excluded.",
)
@click.option(
    "--min-download",
    "--min-ingress",
    "min_download_mbps",
    type=float,
    default=None,
    help="Minimum Download (Mbps) a node must report; nodes with no figure are excluded.",
)
@click.option("--lat", type=float, help="Latitude for distance filtering")
@click.option("--lon", type=float, help="Longitude for distance filtering")
@click.option("--max-distance", "max_distance", type=int, help="Maximum distance in miles from --lat/--lon")
@click.option(
    "--sort",
    "sort_by",
    type=click.Choice(display.SORT_KEYS + list(display.SORT_KEY_ALIASES)),
    default=None,
    help="Sort result by the chosen field (default: cheapest $/GPU·h first).",
)
@click.option("--limit", type=int, default=None, help="Limit number of rows shown.")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@handle_errors
def ls_command(
    gpu_type: Optional[str],
    gpu_count: Optional[int],
    lat: Optional[float],
    lon: Optional[float],
    max_distance: Optional[int],
    sort_by: Optional[str],
    limit: Optional[int],
    output_format: str,
    json_output: bool,
    min_cuda_version: Optional[float],
    countries: tuple,
    min_vram_gb: Optional[float],
    max_price: Optional[float],
    tier: Optional[str],
    min_cpus: Optional[int],
    nvlink: bool,
    min_download_mbps: Optional[float],
):
    """List available GPU nodes.

    Rows are cheapest $/GPU·h first; nodes without a price come last. ★ marks
    nodes no other node beats: a node more than 10% faster to download wins
    outright; at a similar download speed a node wins by being no worse, and
    better somewhere, on price, VRAM, RAM, disk, PCIe, memory bandwidth, TFLOPS,
    upload and US location (at the same price, bandwidth and US location count
    before the hardware specs). Nodes under 100 Mbps download are never ★.
    --sort picks another key.

    Link shows how the GPUs of a node are wired to each other (NV18 = NVLink with
    18 links, PCIe/SYS = PCIe only, worst class shown); it is "—" until the node's
    validator reports it.

    \b
    Examples:
      lium ls --gpu H100 --count 8
      lium ls --gpu H100 --country US,NL --max-price 2.50
      lium ls --min-vram 80 --min-cuda 12.8 --tier secure
      lium ls --gpu A100 --format json | jq '.[0].huid'
    """
    output_format = resolve_output_format(output_format, json_output)

    _, error = validation.validate(
        limit, lat, lon, max_distance, min_cuda_version,
        min_vram_gb=min_vram_gb, max_price=max_price, min_cpus=min_cpus, min_download_mbps=min_download_mbps,
    )
    if error:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)
    filters = node_filters.NodeFilters(
        countries=node_filters.parse_countries(countries),
        min_vram_gb=min_vram_gb,
        max_price_per_gpu_hour=max_price,
        tier=tier,
    )

    # Load data
    lium = Lium()
    ctx = {
        "lium": lium,
        "gpu_type": gpu_type,
        "gpu_count": gpu_count,
        "lat": lat,
        "lon": lon,
        "max_distance": max_distance,
        "min_cuda_version": min_cuda_version,
        "min_cpus": min_cpus,
        "nvlink": nvlink,
        "min_download_mbps": min_download_mbps,
    }

    action = GetExecutorsAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading nodes", lambda: action.execute(ctx))

    executors = node_filters.apply(result.data["executors"], filters)

    # Check if empty
    if not executors:
        if output_format == "json":
            click.echo("[]")
            return
        if filters.active and result.data["executors"]:
            ui.error(f"No nodes match {node_filters.describe(filters)}")
            ui.info(f"Tip: loosen a filter, or {ui.styled('lium ls --gpu ' + gpu_type if gpu_type else 'lium ls', 'success')} to see everything")
            return
        known = lium.unknown_gpu_type(gpu_type) if gpu_type else None
        if known is not None:
            ui.error(f"No GPU type matches '{escape(gpu_type)}'")
            ui.info(f"Types on the marketplace: {', '.join(known)}")
            ui.info(f"Tip: {ui.styled('lium ls --gpu RTX4090', 'success')} {ui.styled('# or just 4090', 'dim')}")
            return
        if nvlink or min_download_mbps is not None:
            wanted = [w for w in (
                "NVLink between every GPU pair" if nvlink else None,
                f"Download ≥ {min_download_mbps:g} Mbps" if min_download_mbps is not None else None,
            ) if w]
            ui.error(f"No available node reports {' and '.join(wanted)}")
            # each filter's rule, as the help text states it: --nvlink needs a topology report; --min-download
            # judges the Download (Mbps) column
            rules = [r for r in (
                "--nvlink excludes nodes with no topology report yet" if nvlink else None,
                "--min-download judges the Download (Mbps) column" if min_download_mbps is not None else None,
            ) if r]
            if nvlink:
                tail = f"Drop the filter and check on the pod: {ui.styled('nvidia-smi topo -m', 'success')}"
            else:
                tail = "Drop the filter or lower the floor"
            ui.info(f"{'; '.join(rules)}. {tail}")
            return
        if gpu_type:
            ui.error(f"All {gpu_type} GPUs are currently rented out")
            ui.info(f"Tip: {ui.styled('lium ls', 'success')}")
        else:
            ui.error("All GPUs are currently rented out")
            ui.info("Check back later or contact support if this persists")
        show_workspace(lium)
        return


    if output_format == "json":
        sorted_executors, pareto_flags = display.sort_executors(
            executors, sort_by=sort_by, limit=limit
        )
        payload = [
            display.compact_executor(exe, is_pareto, idx)
            for idx, (exe, is_pareto) in enumerate(zip(sorted_executors, pareto_flags), 1)
        ]
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        store_executor_selection(sorted_executors)
        return

    # Build table
    table, sorted_executors, header, tip = display.build_executors_table(
        executors,
        sort_by=sort_by,
        limit=limit,
        width=console.width,
    )

    # Display
    ui.info(header)
    ui.print(table)
    _, hidden = display.fit_columns(console.width)
    if hidden:
        ui.dim(display.format_hidden_columns(hidden))
    ui.print("")
    ui.info(tip)
    show_workspace(lium)

    # Store selection for index-based access in up command
    store_executor_selection(sorted_executors)
