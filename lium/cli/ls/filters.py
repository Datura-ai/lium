"""Client-side node filters for `lium ls` that the API does not offer as query parameters."""

from dataclasses import dataclass
from typing import Iterable, List, Optional

from lium.sdk import ExecutorInfo
from . import display


@dataclass(frozen=True)
class NodeFilters:
    countries: tuple = ()             # ISO codes or names, case-insensitive; any of them
    min_vram_gb: Optional[float] = None
    max_price_per_gpu_hour: Optional[float] = None
    tier: Optional[str] = None        # "spot" or "secure"

    @property
    def active(self) -> bool:
        return bool(self.countries) or self.min_vram_gb is not None \
            or self.max_price_per_gpu_hour is not None or self.tier is not None


def parse_countries(values: Iterable[str]) -> tuple:
    """``("US,NL", "germany")`` -> ``("us", "nl", "germany")``."""
    out = []
    for value in values:
        for part in value.split(","):
            part = part.strip().lower()
            if part:
                out.append(part)
    return tuple(out)


def _country_matches(executor: ExecutorInfo, wanted: tuple) -> bool:
    location = executor.location or {}
    code = (location.get("country_code") or location.get("iso_code") or "").strip().lower()
    name = (location.get("country") or "").strip().lower()
    for candidate in wanted:
        # A two-letter input is an ISO code: matched exactly, never as a name
        # prefix (DE would otherwise take Denmark, CH China and Chile).
        prefix_ok = len(candidate) > 2 and name.startswith(candidate)
        if candidate == code or (name and (name == candidate or prefix_ok)):
            return True
    return False


def vram_gb(executor: ExecutorInfo) -> Optional[int]:
    """Per-GPU memory in GB as the table and JSON show it (an 81559 MiB H100 is "80")."""
    detail = display._first_gpu_detail(executor.specs)
    capacity = display._intish(detail.get("capacity"))
    return round(capacity / 1024) if capacity else None


def keep(executor: ExecutorInfo, filters: NodeFilters) -> bool:
    if filters.countries and not _country_matches(executor, filters.countries):
        return False
    if filters.min_vram_gb is not None:
        vram = vram_gb(executor)
        if vram is None or vram < filters.min_vram_gb:
            return False
    if filters.max_price_per_gpu_hour is not None:
        # The SDK stores a missing price as 0, so "no price" is falsy, not None;
        # a node with no price must not pass every --max-price as the cheapest.
        if not executor.price_per_gpu or executor.price_per_gpu > filters.max_price_per_gpu_hour:
            return False
    if filters.tier is not None and (executor.tier or "").lower() != filters.tier.lower():
        return False
    return True


def apply(executors: List[ExecutorInfo], filters: NodeFilters) -> List[ExecutorInfo]:
    if not filters.active:
        return list(executors)
    return [e for e in executors if keep(e, filters)]


def describe(filters: NodeFilters) -> str:
    """``country US/NL, VRAM ≥ 80 GB, ≤ $2.00/GPU·h, tier spot`` for the empty-result message."""
    parts = []
    if filters.countries:
        parts.append("country " + "/".join(c.upper() if len(c) == 2 else c for c in filters.countries))
    if filters.min_vram_gb is not None:
        parts.append(f"VRAM ≥ {filters.min_vram_gb:g} GB")
    if filters.max_price_per_gpu_hour is not None:
        parts.append(f"≤ ${filters.max_price_per_gpu_hour:.2f}/GPU·h")
    if filters.tier:
        parts.append(f"tier {filters.tier}")
    return ", ".join(parts)
