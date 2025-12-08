"""Display formatting logic for ls command."""

from typing import Any, Callable, Dict, List, Optional

from rich.table import Table

from lium.sdk import ExecutorInfo
from lium.cli.utils import console, calculate_pareto_frontier


def _mid_ellipsize(s: str, width: int = 28) -> str:
    """Truncate string with middle ellipsis if too long."""
    if not s:
        return "—"
    if len(s) <= width:
        return s
    keep = width - 1
    left = keep // 2
    right = keep - left
    return f"{s[:left]}…{s[-right:]}"


def _cfg(exe: ExecutorInfo) -> str:
    """Format GPU configuration string."""
    return f"{exe.available_gpu_count}×{exe.gpu_type}"


def _country_name(loc: Optional[Dict]) -> str:
    """Extract country name from location dict."""
    if not loc:
        return "—"
    country = (loc.get("country") or "").strip()
    if country:
        return country
    code = (loc.get("country_code") or loc.get("iso_code") or "").strip()
    return code.upper() if code else "—"


def _money(v: Optional[float]) -> str:
    """Format money value with fixed width."""
    return f"{v:>6.2f}" if v is not None else "—"


def _intish(x: Any) -> Optional[int]:
    """Convert to int safely."""
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _maybe_int(x: Any) -> str:
    """Convert to int string or dash."""
    v = _intish(x)
    return str(v) if v is not None else "—"


def _maybe_gi_from_capacity(capacity: Any) -> str:
    """Convert MiB to GiB."""
    v = _intish(capacity)
    return str(round(v / 1024)) if v else "—"


def _maybe_gi_from_big_number(n: Any) -> str:
    """Convert KiB to GiB."""
    v = _intish(n)
    if v is None:
        return "—"
    if v < 8192:  # Already GiB
        return str(v)
    return str(round(v / (1024 * 1024)))


def _first_gpu_detail(specs: Optional[Dict]) -> Dict:
    """Get first GPU detail from specs."""
    if not specs:
        return {}
    gpu = specs.get("gpu", {})
    details = gpu.get("details", [])
    return details[0] if details else {}


def _specs_row(specs: Optional[Dict]) -> Dict[str, str]:
    """Extract display fields from specs."""
    if not specs:
        return {k: "—" for k in ["VRAM", "RAM", "Disk", "PCIe", "Mem", "TFLOPs", "Upload", "Download", "Ports"]}

    d = _first_gpu_detail(specs)
    ram = specs.get("ram", {})
    disk = specs.get("hard_disk", {})
    net = specs.get("network", {})

    return {
        "VRAM": _maybe_gi_from_capacity(d.get("capacity")),
        "RAM": _maybe_gi_from_big_number(ram.get("total")),
        "Disk": _maybe_gi_from_big_number(disk.get("total")),
        "Country": _country_name(specs.get("location")),
        "PCIe": _maybe_int(d.get("pcie_speed")),
        "Upload": _maybe_int(net.get("upload_speed")),
        "Download": _maybe_int(net.get("download_speed")),
        "Ports": _maybe_int(specs.get("available_port_count")),
    }


def _sort_key_factory(name: str) -> Callable[[ExecutorInfo], Any]:
    """Get sort key function by name."""
    mapping = {
        "price_gpu": lambda e: e.price_per_gpu_hour or 0.0,
        "price_total": lambda e: e.price_per_hour or 0.0,
        "loc": lambda e: _country_name(e.location),
        "id": lambda e: e.huid,
        "gpu": lambda e: (e.gpu_type, e.gpu_count),
    }
    return mapping.get(name, mapping["price_gpu"])


def _add_table_columns(t: Table) -> None:
    """Add columns to the table with fixed widths."""
    t.add_column("", justify="right", width=3, no_wrap=True, style="dim")
    t.add_column("Id", justify="left", ratio=8, min_width=18, overflow="fold")
    t.add_column("Config", justify="left", width=12, no_wrap=True)
    t.add_column("GPU Splitting", justify="left", min_width=12, no_wrap=True)
    t.add_column("$/GPU·h", justify="right", width=8, no_wrap=True)
    t.add_column("Location", justify="left", ratio=4, min_width=10, overflow="fold")
    t.add_column("VRAM (Gb)", justify="right", width=11, no_wrap=True)
    t.add_column("RAM (Gb)", justify="right", width=10, no_wrap=True)
    t.add_column("Disk (Gb)", justify="right", width=11, no_wrap=True)
    t.add_column("Upload (Mbps)", justify="right", width=14, no_wrap=True)
    t.add_column("Download (Mbps)", justify="right", width=16, no_wrap=True)
    t.add_column("Ports", justify="left", ratio=3, min_width=5, overflow="fold")


def format_header(executor_count: int, pareto_count: int, show_pareto: bool) -> str:
    """Format header text for executors list."""
    if show_pareto and pareto_count > 0:
        return f"Executors  ({executor_count} shown, ★ {pareto_count} optimal)"
    else:
        return f"Executors  ({executor_count} shown)"


def format_tip() -> str:
    """Format tip message."""
    return f"Tip: {console.get_styled('lium up <index>', 'success')} {console.get_styled('# e.g. lium up 1', 'dim')}"


def build_executors_table(
    executors: List[ExecutorInfo],
    sort_by: str = "price_gpu",
    limit: Optional[int] = None,
    show_pareto: bool = True
) -> tuple[Table, List[ExecutorInfo], str, str]:
    """Build executors table, returns (table, sorted_executors, header, tip)."""

    if not executors:
        return None, [], "", ""

    # Calculate Pareto frontier before sorting/limiting
    pareto_flags = calculate_pareto_frontier(executors) if show_pareto else [False] * len(executors)

    # Combine executors with their Pareto status for sorting
    executors_with_pareto = list(zip(executors, pareto_flags))

    # Sort with Pareto-optimal first, then by chosen criteria
    if show_pareto:
        executors_with_pareto = sorted(
            executors_with_pareto,
            key=lambda x: (not x[1], _sort_key_factory(sort_by)(x[0]))
        )
    else:
        executors_with_pareto = sorted(
            executors_with_pareto,
            key=lambda x: _sort_key_factory(sort_by)(x[0])
        )

    # Apply limit
    if isinstance(limit, int) and limit > 0:
        executors_with_pareto = executors_with_pareto[:limit]

    # Extract sorted executors and their Pareto flags
    sorted_executors = [e for e, _ in executors_with_pareto]
    pareto_flags = [p for _, p in executors_with_pareto]

    # Count Pareto-optimal in shown results
    pareto_count = sum(pareto_flags)

    # Build table
    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )
    _add_table_columns(table)

    # Add rows
    for idx, (exe, is_pareto) in enumerate(zip(sorted_executors, pareto_flags), 1):
        s = _specs_row(exe.specs)

        # Format HUID with Pareto star
        huid = _mid_ellipsize(exe.huid)
        huid += " (DinD)" if exe.docker_in_docker else ""
        huid_display = f"{console.get_styled('★', 'success')} {console.get_styled(huid, 'id')}" if is_pareto else f"  {console.get_styled(huid, 'id')}"
        gpu_splitting_display = f"Min GPUs: {exe.min_gpu_count_for_rental}" if exe.min_gpu_count_for_rental else "—"

        table.add_row(
            str(idx),
            huid_display,
            _cfg(exe),
            gpu_splitting_display,
            console.get_styled(_money(exe.price_per_gpu_hour), 'success'),
            _country_name(exe.location),
            s["VRAM"],
            s["RAM"],
            s["Disk"],
            s["Upload"],
            s["Download"],
            s["Ports"]
        )

    header = format_header(len(sorted_executors), pareto_count, show_pareto)
    tip = format_tip()

    return table, sorted_executors, header, tip
