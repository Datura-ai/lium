"""Sorting and filtering for `lium ps`, on the same fields `--format json` prints."""

from typing import Dict, Iterable, List, Optional, Tuple

from lium.sdk import PodInfo
from . import display

SORT_KEYS = ("created", "name", "status", "price", "spent", "gpu", "uptime")

# Fields a `--filter KEY=VALUE` may name; values are compared case-insensitively
# as prefixes, so `status=run` matches RUNNING and `gpu=H1` matches H100.
FILTER_KEYS = ("status", "name", "huid", "gpu", "gpu_type", "template", "id")

_FILTER_ALIASES = {"gpu": "gpu_type"}


def parse_filters(specs: Iterable[str]) -> List[Tuple[str, str]]:
    """``["status=RUNNING", "gpu=H100"]`` -> ``[("status", "RUNNING"), ("gpu_type", "H100")]``."""
    parsed = []
    for spec in specs:
        key, sep, value = spec.partition("=")
        key = key.strip().lower()
        if not sep or key not in FILTER_KEYS:
            raise ValueError(
                f"Bad filter '{spec}': use KEY=VALUE with KEY one of {', '.join(FILTER_KEYS)}"
            )
        parsed.append((_FILTER_ALIASES.get(key, key), value.strip()))
    return parsed


def matches(pod: PodInfo, filters: List[Tuple[str, str]]) -> bool:
    if not filters:
        return True
    view: Dict[str, object] = display.compact_pod(pod)
    for key, wanted in filters:
        actual = view.get(key)
        if actual is None or not str(actual).lower().startswith(wanted.lower()):
            return False
    return True


def _sort_value(pod: PodInfo, key: str):
    view = display.compact_pod(pod)
    if key in ("created", "uptime"):
        # created: newest first; uptime: longest running first. Both read created_at.
        return view["created_at"] or ""
    if key == "name":
        return (view["name"] or view["huid"] or "").lower()
    if key == "status":
        return (view["status"] or "").lower()
    if key == "price":
        return view["price_per_hour"] if view["price_per_hour"] is not None else -1.0
    if key == "spent":
        return view["spent_usd"] if view["spent_usd"] is not None else -1.0
    if key == "gpu":
        return ((view["gpu_type"] or "").lower(), view["gpu_count"] or 0)
    raise ValueError(f"Unknown sort key '{key}': use one of {', '.join(SORT_KEYS)}")


def sort_pods(pods: List[PodInfo], key: Optional[str], reverse: bool = False) -> List[PodInfo]:
    """Sorted copy. Money largest first, created newest first, uptime longest first, names A→Z."""
    if not key:
        return list(pods)
    descending_by_default = key in ("price", "spent", "created")
    return sorted(pods, key=lambda p: _sort_value(p, key), reverse=descending_by_default != reverse)
