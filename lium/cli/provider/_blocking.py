"""Blocking reasons for ``lium provider node list|get|status`` and ``lium provider status``.

The portal serves ``blocking_reasons`` per node: everything that keeps the node off the listing or
out of idle pay, each with its message, the measured and required value and the exact fix. Until
the portal serves that list, it is rebuilt here from what the portal already returns: the
validator's ``computed_status.last_error``, the listing's ``hidden_reasons`` and the idle-pay
reasons of ``GET /miners/overview``. A node with any reason gets a red BLOCKING panel; ``--json``
carries the same list under each node's ``blocking_reasons``. An entry with ``gating: false`` (an
idle-pay code Secure does not require) blocks nothing: it prints as "Not eligible for idle pay: …"
with its "No action: …" line.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from rich.console import Group
from rich.markup import escape
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

from lium.cli.utils import console
from lium.provider.errors import ProviderError

# The validator's idle-pay codes (lium-io ``ZeroIncentiveReason``) a Secure listing requires,
# the portal's default gating list. Not listed on purpose: the two ``NOT_GATED`` codes below,
# ``spot_tier`` and ``new_rentals_paused``.
SECURE_GATING_CODES = frozenset(
    {
        "nvidia_driver_below_minimum",
        "sysbox_not_enabled",
        "insufficient_disk_for_vram",
        "flagship_without_ncu_or_split",
        "cannot_apply_gpu_power_cap",
        "outdated_executor_image",
        "provider_discord_not_connected",
        "price_above_market_p90_soft_limit",
        "banned_network_abuse",
        "miner_default_job",
        "port_limited_remainder",
    }
)

# Idle-pay codes Secure does not require: shown as "Not eligible for idle pay: …" with a "No action: …" line,
# never in the BLOCKING panel and never counted as blocked (``gating: false``). Same words as the portal.
NOT_GATED: dict[str, tuple[str, str]] = {
    "gpu_model_not_eligible_for_unrented_incentive": (
        "This GPU model is not in the idle-pay program",
        "No action: this model earns from rentals only.",
    ),
    "no_unrented_capacity_for_gpu_count": (
        "No idle-pay room for this node size right now",
        "No action: room for this size is full. Rentals still pay, and room opens as the market moves.",
    ),
}
_ALIASES = {
    "price_above_p90": "price_above_market_p90_soft_limit",
    "gpu_model_not_eligible": "gpu_model_not_eligible_for_unrented_incentive",
    "no_unrented_capacity": "no_unrented_capacity_for_gpu_count",
}

# lium-io ``incentive/default.py::get_min_driver_multiplier``: the lowest driver that earns idle pay.
MIN_NVIDIA_DRIVER = "580.65.06"

# Listing states the provider chose; the node is hidden on purpose, not blocked.
_PROVIDER_CHOSEN_HIDDEN = frozenset({"NEW_RENTALS_PAUSED", "RECLAIMING"})
_HEALTHY_STATUSES = frozenset({"AVAILABLE", "RENTED"})

_HIDDEN_FIXES: dict[str, str] = {
    "NOT_RESPONDING": "Start the executor (`docker compose up -d` in neurons/executor) and open its port to the internet.",
    "NOT_ACTIVE": "Wait for the validator's next check, or fix the error it reported (`lium provider node status <id>`).",
    "NOT_VERIFIED": "Wait for the validator's next check, or fix the error it reported (`lium provider node status <id>`).",
    "DISK_TOO_FULL": "Free disk on the node until it is at most 90% used.",
    "DISK_FREE_TOO_LOW": "Free disk on the node or add a larger disk.",
    "DISK_NOT_REPORTED": "Update and restart the executor so it reports its disk.",
    "NETWORK_TOO_SLOW": "Move the node to a faster uplink.",
    "GPU_COUNT_UNKNOWN": "Restart the executor so the validator reads its GPUs on the next check.",
}


def _num(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _idle_pay_entry(code: str, context: Mapping[str, Any], message: str | None, node_id: str) -> dict[str, Any]:
    """One validator idle-pay reason as ``{title, measured, required, fix}``."""
    ctx = context or {}
    node = node_id or "<id>"
    title, measured, required, fix = message or code, None, None, message or code
    if code == "nvidia_driver_below_minimum":
        minimum = MIN_NVIDIA_DRIVER
        title = "NVIDIA driver below the network minimum"
        measured = ctx.get("nvidia_driver_version")
        required = f"{minimum} or newer"
        fix = (
            f"Upgrade the NVIDIA driver on this node to {minimum} or newer, reboot, "
            "then restart the executor (`docker compose up -d` in neurons/executor)."
        )
    elif code == "insufficient_disk_for_vram":
        title = "Not enough disk for the GPUs' VRAM"
        if ctx.get("total_disk_gb") is not None:
            measured = f"{_num(ctx['total_disk_gb'])} GB disk"
        if ctx.get("required_disk_gb") is not None:
            required = f"{_num(ctx['required_disk_gb'])} GB disk"
            fix = f"Give this node at least {_num(ctx['required_disk_gb'])} GB of total disk."
        else:
            fix = "Give this node more total disk than its GPUs' VRAM requires."
    elif code == "price_above_market_p90_soft_limit":
        title = "Price above the market's soft limit"
        if ctx.get("price_per_gpu") is not None:
            measured = f"${_num(ctx['price_per_gpu'])}/GPU·h"
        limit = ctx.get("soft_limit_threshold")
        if limit is not None:
            required = f"${_num(limit)}/GPU·h or less"
            fix = f"`lium provider node update-price {node} --price {_num(limit)}`"
        else:
            fix = f"Lower the price: `lium provider node update-price {node} --price <price>`"
    elif code == "sysbox_not_enabled":
        title = "sysbox runtime not enabled"
        measured = ctx.get("sysbox_runtime") or "not enabled"
        required = "sysbox runtime"
        fix = "Install and enable sysbox on the host, then restart the executor (`docker compose up -d` in neurons/executor)."
    elif code == "flagship_without_ncu_or_split":
        title = "Idle flagship node offers no NCU profiling, GPU splitting or confidential computing"
        required = "NCU profiling, GPU splitting or confidential computing"
        fix = (
            "Open the profiling counters (NVreg_RestrictProfilingToAdminUsers=0, then reboot), or set a "
            f"minimum GPU count below the full node (`lium provider node min-gpu set {node} <n>`), "
            "or run the executor in a confidential VM."
        )
    elif code == "cannot_apply_gpu_power_cap":
        title = "Executor cannot set a GPU power limit"
        required = "nvidia-smi -pl works in the executor container"
        fix = (
            "Run `docker compose pull && docker compose up -d` in neurons/executor; a custom compose "
            "needs `privileged: true` on the executor container."
        )
    elif code == "outdated_executor_image":
        title = "Executor image is outdated"
        measured = ctx.get("observed_digest")
        required = ctx.get("expected_ref") or ctx.get("expected_digest")
        fix = message or "Run `docker compose pull && docker compose up -d` in neurons/executor."
    elif code == "provider_discord_not_connected":
        title = "Provider Discord not connected"
        measured, required = "not connected", "connected"
        fix = "`lium provider config connect-discord`"
    elif code == "banned_network_abuse":
        title = "Banned for network abuse"
        fix = "Contact Lium support; a banned node neither lists nor earns."
    elif code == "miner_default_job":
        title = "Node runs your own default job"
        required = "a Lium job"
        fix = "Stop your own default job on this node so it runs Lium jobs."
    elif code == "port_limited_remainder":
        title = "Too few free ports for the free GPUs"
        if ctx.get("available_port_count") is not None:
            measured = f"{_num(ctx['available_port_count'])} free ports"
        if ctx.get("required_port_count") is not None:
            required = f"{_num(ctx['required_port_count'])} free ports"
        fix = "Open more ports on the node, or wait for the rental to end."
    return {
        "code": code,
        "title": title,
        "measured": measured,
        "required": required,
        "fix": fix,
        "secure": code in SECURE_GATING_CODES,
        "gating": True,
        "source": "idle_pay",
    }


def _not_gated_entry(code: str) -> dict[str, Any]:
    title, fix = NOT_GATED[code]
    return {
        "code": code,
        "title": title,
        "measured": None,
        "required": None,
        "fix": fix,
        "secure": False,
        "gating": False,
        "source": "idle_pay",
    }


def fallback_reasons(row: Mapping[str, Any], idle_pay_reasons: Iterable[Mapping[str, Any]] = ()) -> list[dict[str, Any]]:
    """The node's blocking reasons from ``last_error``, ``hidden_reasons`` and the idle-pay reasons."""
    node_id = str(row.get("id") or row.get("executor_id") or "")
    reasons: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(entry: dict[str, Any]) -> None:
        key = str(entry.get("code") or entry.get("title"))
        if key in seen:
            return
        seen.add(key)
        reasons.append(entry)

    computed = row.get("computed_status")
    if isinstance(computed, Mapping) and str(computed.get("status") or "") not in _HEALTHY_STATUSES:
        err = computed.get("last_error")
        if isinstance(err, Mapping):
            _add(
                {
                    "code": err.get("reason_code") or "LAST_ERROR",
                    "title": err.get("title") or err.get("message") or "Validator check failed",
                    "measured": None,
                    "required": None,
                    "fix": err.get("remediation") or err.get("message") or "",
                    "secure": False,
                    "gating": True,
                    "source": "last_error",
                }
            )

    for hidden in row.get("hidden_reasons") or []:
        if not isinstance(hidden, Mapping):
            continue
        code = str(hidden.get("code") or "")
        if not code or code in _PROVIDER_CHOSEN_HIDDEN:
            continue
        message = str(hidden.get("message") or code)
        _add(
            {
                "code": code,
                "title": message,
                "measured": None,
                "required": None,
                "fix": _HIDDEN_FIXES.get(code, message).replace("<id>", node_id or "<id>"),
                "secure": False,
                "gating": True,
                "source": "hidden_reason",
            }
        )

    for idle in idle_pay_reasons or []:
        if not isinstance(idle, Mapping):
            continue
        code = str(idle.get("code") or idle.get("reason") or "")
        code = _ALIASES.get(code, code)
        if code in NOT_GATED:
            _add(_not_gated_entry(code))
            continue
        if code not in SECURE_GATING_CODES:
            continue
        context = idle.get("context") if isinstance(idle.get("context"), Mapping) else {}
        message = idle.get("message") or idle.get("message_for_miner")
        _add(_idle_pay_entry(code, context, str(message) if message else None, node_id))
    return reasons


def _first(entry: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = entry.get(key)
        if value not in (None, ""):
            return value
    return None


def normalise(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One portal ``blocking_reasons`` entry in the renderer's field names; unknown names pass through."""
    code = str(_first(entry, "code", "reason_code") or "")
    code = _ALIASES.get(code, code)
    secure = _first(entry, "secure", "secure_requirement", "blocks_secure", "gates_secure")
    gating = entry.get("gating")
    return {
        "code": code,
        "title": _first(entry, "title", "message") or code,
        "measured": _first(entry, "measured", "measured_value"),
        "required": _first(entry, "required", "required_value"),
        "fix": _first(entry, "fix", "exact_fix", "fix_text", "remediation") or "",
        "secure": bool(secure) if secure is not None else code in SECURE_GATING_CODES,
        "gating": bool(gating) if gating is not None else code not in NOT_GATED,
    }


def _all_reasons(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("blocking_reasons")
    if not isinstance(raw, list):
        return []
    return [normalise(entry) for entry in raw if isinstance(entry, Mapping)]


def node_reasons(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What blocks the node (the BLOCKING panel): the portal's list without its ``gating: false`` entries."""
    return [reason for reason in _all_reasons(row) if reason["gating"]]


def not_eligible_reasons(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Why the node earns no idle pay while nothing needs fixing: the ``gating: false`` entries."""
    return [reason for reason in _all_reasons(row) if not reason["gating"]]


def needs_fallback(rows: Iterable[Mapping[str, Any]]) -> bool:
    return any(isinstance(row, Mapping) and not isinstance(row.get("blocking_reasons"), list) for row in rows)


def idle_pay_reasons_by_node(overview: Mapping[str, Any] | None) -> dict[str, list[Mapping[str, Any]]]:
    """``GET /miners/overview`` -> ``{executor_id: idle_pay_reasons}``."""
    out: dict[str, list[Mapping[str, Any]]] = {}
    if not isinstance(overview, Mapping):
        return out
    for row in overview.get("node_rows") or []:
        if not isinstance(row, Mapping) or not row.get("executor_id"):
            continue
        reasons = row.get("idle_pay_reasons")
        if isinstance(reasons, list) and reasons:
            out[str(row["executor_id"])] = reasons
    return out


def fetch_idle_pay_reasons(client: Any) -> dict[str, list[Mapping[str, Any]]]:
    """The overview's idle-pay reasons; an overview that fails leaves only the other two sources."""
    try:
        return idle_pay_reasons_by_node(client.provider_overview())
    except ProviderError:
        return {}


def attach(rows: Iterable[Any], idle_by_node: Mapping[str, list[Mapping[str, Any]]] | None = None) -> None:
    """Give every node dict without a portal ``blocking_reasons`` the list rebuilt from the fallback sources."""
    for row in rows:
        if not isinstance(row, dict) or isinstance(row.get("blocking_reasons"), list):
            continue
        node_id = str(row.get("id") or row.get("executor_id") or "")
        row["blocking_reasons"] = fallback_reasons(row, (idle_by_node or {}).get(node_id, ()))
        row["blocking_reasons_source"] = "cli_fallback"


def blocked_count(rows: Iterable[Any]) -> int:
    return sum(1 for row in rows if isinstance(row, Mapping) and node_reasons(row))


def _node_label(row: Mapping[str, Any]) -> str:
    node_id = str(row.get("id") or row.get("executor_id") or "?")
    gpu_count = row.get("gpu_count")
    gpu_type = row.get("gpu_type")
    gpus = f"{gpu_count}×{gpu_type}" if gpu_count and gpu_type else (gpu_type or "")
    ip = row.get("executor_ip_address")
    port = row.get("executor_ip_port")
    where = f"{ip}:{port}" if ip and port else ""
    return " · ".join(part for part in (node_id, str(gpus), where) if part)


def blocking_panel(row: Mapping[str, Any]) -> Panel | None:
    """The red BLOCKING panel for one node, or None when nothing blocks it."""
    reasons = node_reasons(row)
    if not reasons:
        return None
    lines: list[Text | Padding] = []
    for reason in reasons:
        lines.append(Text.from_markup(f"[bold red]✗ {escape(str(reason['title']))}[/]"))
        figures = []
        if reason.get("measured") not in (None, ""):
            figures.append(f"measured {reason['measured']}")
        if reason.get("required") not in (None, ""):
            figures.append(f"required {reason['required']}")
        if figures:
            lines.append(Padding(Text(" · ".join(figures)), (0, 0, 0, 2)))
        if reason.get("fix"):
            lines.append(Padding(Text.from_markup(f"[bold]Fix:[/] {escape(str(reason['fix']))}"), (0, 0, 0, 2)))
    secure = [r for r in reasons if r.get("secure")]
    if secure and str(row.get("tier") or "secure").lower() != "spot":
        noun = "requirement" if len(secure) == 1 else "requirements"
        lines.append(Text(""))
        lines.append(
            Text.from_markup(
                f"[bold red]Secure listing: {len(secure)} unmet {noun}[/] "
                "(Secure lists only nodes that earn idle pay)"
            )
        )
        for reason in secure:
            lines.append(Padding(Text(f"• {reason['title']}"), (0, 0, 0, 2)))
    return Panel(
        Group(*lines),
        title=Text.from_markup(f"[bold red]BLOCKING[/] [red]{escape(_node_label(row))}[/]"),
        title_align="left",
        border_style="red",
        expand=True,
    )


def print_panels(rows: Iterable[Any]) -> int:
    """Print one BLOCKING panel per blocked node; returns how many were printed."""
    printed = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        panel = blocking_panel(row)
        if panel is not None:
            console.print(panel)
            printed += 1
    return printed


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def print_not_eligible(rows: Iterable[Any], *, short: bool) -> int:
    """Print the "Not eligible for idle pay" reasons, never in red; ``short`` is one marker line per node (the tables).

    Returns how many nodes had one.
    """
    nodes = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        reasons = not_eligible_reasons(row)
        if not reasons:
            continue
        nodes += 1
        if short:
            node_id = str(row.get("id") or row.get("executor_id") or "?")
            words = "; ".join(_lower_first(str(r["title"])) for r in reasons)
            console.print(Text(f"  ◦ {node_id}: not eligible for idle pay ({words}); no action needed", style="dim"))
            continue
        for reason in reasons:
            console.print(Text.from_markup(f"[bold]Not eligible for idle pay:[/] {escape(_lower_first(str(reason['title'])))}"))
            if reason.get("fix"):
                console.print(Padding(Text(str(reason["fix"])), (0, 0, 0, 2)))
    return nodes


__all__ = [
    "MIN_NVIDIA_DRIVER",
    "NOT_GATED",
    "SECURE_GATING_CODES",
    "attach",
    "blocked_count",
    "blocking_panel",
    "fallback_reasons",
    "fetch_idle_pay_reasons",
    "idle_pay_reasons_by_node",
    "needs_fallback",
    "node_reasons",
    "normalise",
    "not_eligible_reasons",
    "print_not_eligible",
    "print_panels",
]
