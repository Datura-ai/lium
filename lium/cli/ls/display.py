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
    return f"{exe.gpu_count}×{exe.gpu_type}"


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


def _tier_display(exe: ExecutorInfo) -> str:
    """Render tier with a risk-signalling colour: spot (reclaimable, no fee withheld)
    in warning, secure in success, unknown as a dash."""
    tier = (exe.tier or "").strip().lower()
    if tier == "spot":
        return console.get_styled("spot", "warning")
    if tier == "secure":
        return console.get_styled("secure", "success")
    return "—"


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


def _specs_row(executor: ExecutorInfo) -> Dict[str, str]:
    """Extract display fields from an executor."""
    specs = executor.specs
    if not specs:
        return {k: "—" for k in ["VRAM", "RAM", "CPUs", "Disk", "DiskTotal", "PCIe", "Mem", "TFLOPs", "Upload", "Download", "Ports"]}

    d = _first_gpu_detail(specs)
    ram = specs.get("ram", {})
    disk = specs.get("hard_disk", {})

    return {
        "VRAM": _maybe_gi_from_capacity(d.get("capacity")),
        "RAM": _maybe_gi_from_big_number(ram.get("total")),
        "CPUs": _maybe_int((specs.get("cpu") or {}).get("count")),
        # free, not total: a pod gets a share of the host's FREE disk (≈ (free − 20 GB) × its GPU share,
        # 2/3 as the /root volume + 1/3 as /workspace), so the total is the one number a renter never sees
        "Disk": _maybe_gi_from_big_number(disk.get("free")),
        "DiskTotal": _maybe_gi_from_big_number(disk.get("total")),
        "Country": _country_name(specs.get("location")),
        "PCIe": _maybe_int(d.get("pcie_speed")),
        "Upload": _maybe_int(executor.upload_speed or None),
        "Download": _maybe_int(executor.download_speed or None),
        "Ports": _maybe_int(specs.get("available_port_count")),
    }


_SORT_KEY_FUNCS: Dict[str, Callable[[ExecutorInfo], Any]] = {
    # The SDK stores a missing price as 0; sorted as 0 it would be row 1 and `lium up 1` would
    # rent an unpriced node as "the cheapest". Unknown prices go last.
    "price_gpu": lambda e: (not e.price_per_gpu, e.price_per_gpu or 0.0),
    "price_total": lambda e: (not e.price_per_hour, e.price_per_hour or 0.0),
    "loc": lambda e: _country_name(e.location),
    "id": lambda e: e.huid,
    "gpu": lambda e: (e.gpu_type, e.gpu_count),
    "download": lambda e: -(e.specs.get("network", {}).get("download_speed", 0) or 0),
    "upload": lambda e: -(e.specs.get("network", {}).get("upload_speed", 0) or 0),
}

# Aliases let a caller sort by the field name `--format json` emits.
SORT_KEY_ALIASES = {
    "price_per_gpu_hour": "price_gpu",
    "price_per_hour": "price_total",
}

# Cheapest $/GPU·h first: what a renter or an agent scans for (owner, 7 Sep 2026; DAH-3079).
DEFAULT_SORT_KEY = "price_gpu"
SORT_KEYS = list(_SORT_KEY_FUNCS)


def _sort_key_factory(name: str) -> Callable[[ExecutorInfo], Any]:
    """Get sort key function by name."""
    name = SORT_KEY_ALIASES.get(name, name)
    return _SORT_KEY_FUNCS.get(name, _SORT_KEY_FUNCS[DEFAULT_SORT_KEY])


# (header, nominal width, priority, column options) in display order. Priority
# None = shown at any width: the index, the Id to rent, the GPU config, the price
# and the country. The rest is kept in priority order (1 first) while the terminal
# has room, and dropped from the end when it has not — Rich would otherwise squeeze
# every column evenly, which at 80 columns hides Id and prints the price as "0…".
# Nominal widths: Id fits "★ golden-matrix-ff (DinD)", Location most country names;
# both grow with the terminal. `ratio` is the nominal width plus the 2-char padding, so
# Rich splits the slack in the proportion fit_columns reserved; with a smaller ratio a
# no-slack fit gave Location 12 chars and "United States" folded onto a second line.
_COLUMNS = [
    ("", 3, None, dict(justify="right", width=3, no_wrap=True, style="dim")),
    ("Id", 25, None, dict(justify="left", ratio=27, min_width=25, overflow="fold")),
    ("Config", 12, None, dict(justify="left", width=12, no_wrap=True)),
    ("Tier", 8, 1, dict(justify="left", width=8, no_wrap=True)),
    ("Max CUDA", 10, 4, dict(justify="right", width=10, no_wrap=True)),
    ("$/GPU·h", 8, None, dict(justify="right", width=8, no_wrap=True)),
    ("Location", 13, None, dict(justify="left", ratio=15, min_width=13, overflow="fold")),
    ("VRAM (Gb)", 11, 3, dict(justify="right", width=11, no_wrap=True)),
    ("RAM (Gb)", 10, 6, dict(justify="right", width=10, no_wrap=True)),
    ("CPUs", 5, 7, dict(justify="right", width=5, no_wrap=True)),
    ("Disk free (Gb)", 14, 8, dict(justify="right", width=14, no_wrap=True)),
    ("Upload (Mbps)", 14, 5, dict(justify="right", width=14, no_wrap=True)),
    ("Download (Mbps)", 16, 2, dict(justify="right", width=16, no_wrap=True)),
    ("Ports", 5, 9, dict(justify="left", ratio=7, min_width=5, overflow="fold")),
]
_COLUMN_GAP = 2  # padding=(0, 1) on both sides of a cell, pad_edge=False


def fit_columns(width: Optional[int]) -> tuple[List[str], List[str]]:
    """Split the table headers into (shown, hidden) for a terminal ``width`` wide.

    ``None`` shows everything. Otherwise the always-on columns are placed first,
    then optional columns in priority order until the next one no longer fits.
    """
    if width is None:
        return [h for h, *_ in _COLUMNS], []
    shown = [c for c in _COLUMNS if c[2] is None]
    used = sum(c[1] for c in shown) + _COLUMN_GAP * (len(shown) - 1)
    for column in sorted((c for c in _COLUMNS if c[2] is not None), key=lambda c: c[2]):
        if used + _COLUMN_GAP + column[1] > width:
            break
        shown.append(column)
        used += _COLUMN_GAP + column[1]
    headers = [c[0] for c in _COLUMNS if c in shown]
    return headers, [c[0] for c in _COLUMNS if c not in shown]


def format_hidden_columns(hidden: List[str]) -> str:
    """Footer saying how many columns a narrow terminal dropped."""
    return (
        f"{len(hidden)} more column{'s' if len(hidden) != 1 else ''} hidden "
        f"— widen the terminal or use {console.get_styled('--format json', 'success')}"
    )


def _add_table_columns(t: Table, headers: List[str]) -> None:
    """Add the chosen columns to the table, in display order."""
    for header, _, _, options in _COLUMNS:
        if header in headers:
            t.add_column(header, **options)


def format_header(executor_count: int, pareto_count: int, show_pareto: bool) -> str:
    """Format header text for executors list."""
    if show_pareto and pareto_count > 0:
        return f"Nodes  ({executor_count} shown, ★ {pareto_count} optimal)"
    else:
        return f"Nodes  ({executor_count} shown)"


def format_tip() -> str:
    """Format tip message."""
    return (
        f"Tip: {console.get_styled('lium up <index>', 'success')} {console.get_styled('# e.g. lium up 1', 'dim')}\n"
        f"{console.get_styled('default order: cheapest $/GPU·h first; --sort picks another key', 'dim')}\n"
        f"{console.get_styled('★ = no other node beats it: a 10% faster download wins outright, else better on price and specs (VRAM, RAM, disk, PCIe, memory bandwidth, TFLOPS, upload, US location)', 'dim')}"
    )


def compact_executor(exe: ExecutorInfo, is_pareto: bool, index: int) -> Dict[str, Any]:
    """Slim, table-equivalent JSON view of an executor."""
    s = _specs_row(exe)
    return {
        "index": index,
        "id": exe.id,
        "huid": exe.huid,
        "config": _cfg(exe),
        "gpu_type": exe.gpu_type,
        "gpu_count": exe.gpu_count,
        "price_per_gpu_hour": exe.price_per_gpu,
        "price_per_hour": exe.price_per_hour,
        "country": _country_name(exe.location),
        "country_code": ((exe.location or {}).get("country_code") or (exe.location or {}).get("iso_code") or None),
        "city": (exe.location or {}).get("city") or None,
        "vram_gb": _intish(s["VRAM"]),
        "ram_gb": _intish(s["RAM"]),
        "cpu_count": _intish(s["CPUs"]),
        "disk_gb": _intish(s["Disk"]),
        "disk_total_gb": _intish(s["DiskTotal"]),
        "upload_mbps": _intish(s["Upload"]),
        "download_mbps": _intish(s["Download"]),
        "available_ports": _intish(s["Ports"]),
        "docker_in_docker": exe.docker_in_docker,
        "is_pareto": is_pareto,
        "max_cuda_version": exe.max_cuda_version,
        "tier": exe.tier,
        "machine_name": getattr(exe, "machine_name", None),
    }


def sort_executors(
    executors: List[ExecutorInfo],
    sort_by: Optional[str] = None,
    limit: Optional[int] = None,
    show_pareto: bool = True,
) -> tuple[List[ExecutorInfo], List[bool]]:
    """Sort and limit. Returns (sorted_executors, pareto_flags).

    The default is cheapest $/GPU·h first; the ★ marks the Pareto-optimal nodes
    wherever they land instead of pulling them above cheaper ones (DAH-3079).
    """
    if not executors:
        return [], []

    pareto_flags = calculate_pareto_frontier(executors) if show_pareto else [False] * len(executors)
    pairs = list(zip(executors, pareto_flags))
    sort_key = _sort_key_factory(sort_by or DEFAULT_SORT_KEY)
    pairs.sort(key=lambda x: sort_key(x[0]))

    if isinstance(limit, int) and limit > 0:
        pairs = pairs[:limit]

    return [e for e, _ in pairs], [p for _, p in pairs]


def build_executors_table(
    executors: List[ExecutorInfo],
    sort_by: Optional[str] = None,
    limit: Optional[int] = None,
    show_pareto: bool = True,
    width: Optional[int] = None,
) -> tuple[Table, List[ExecutorInfo], str, str]:
    """Build executors table, returns (table, sorted_executors, header, tip).

    ``width`` is the terminal width the table has to fit (see ``fit_columns``);
    ``None`` keeps every column.
    """

    if not executors:
        return None, [], "", ""

    headers, _ = fit_columns(width)

    sorted_executors, pareto_flags = sort_executors(
        executors, sort_by=sort_by, limit=limit, show_pareto=show_pareto
    )

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
    _add_table_columns(table, headers)

    # Add rows
    for idx, (exe, is_pareto) in enumerate(zip(sorted_executors, pareto_flags), 1):
        s = _specs_row(exe)

        # Format HUID with Pareto star
        huid = _mid_ellipsize(exe.huid)
        huid += " (DinD)" if exe.docker_in_docker else ""
        huid_display = f"{console.get_styled('★', 'success')} {console.get_styled(huid, 'id')}" if is_pareto else f"  {console.get_styled(huid, 'id')}"

        # Style download speed in yellow when below 100 Mbps
        dl_val = _intish(s["Download"])
        dl_display = (
            console.get_styled(s["Download"], "warning")
            if dl_val is not None and dl_val < 100
            else s["Download"]
        )

        cuda_display = f"{exe.max_cuda_version:.1f}" if exe.max_cuda_version is not None else "-"

        cells = {
            "": str(idx),
            "Id": huid_display,
            "Config": _cfg(exe),
            "Tier": _tier_display(exe),
            "Max CUDA": cuda_display,
            "$/GPU·h": console.get_styled(_money(exe.price_per_gpu), 'success'),
            "Location": _country_name(exe.location),
            "VRAM (Gb)": s["VRAM"],
            "RAM (Gb)": s["RAM"],
            "CPUs": s["CPUs"],
            "Disk free (Gb)": s["Disk"],
            "Upload (Mbps)": s["Upload"],
            "Download (Mbps)": dl_display,
            "Ports": s["Ports"],
        }
        table.add_row(*(cells[h] for h in headers))

    header = format_header(len(sorted_executors), pareto_count, show_pareto)
    tip = format_tip()

    return table, sorted_executors, header, tip
