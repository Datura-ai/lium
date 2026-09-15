"""Display formatting logic for rm command."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from lium.sdk import PodInfo
from lium.cli.ps.display import _parse_timestamp, format_duration
from lium.cli.spend.report import NOT_YET_BILLING


def pod_spend(pod: PodInfo, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Uptime and estimated spend of a pod: uptime × list $/h.

    The API does not return a billed figure, so ``spent_is_estimate`` is always
    true; ``None`` marks the fields that cannot be computed (no timestamp, no
    price). ``uptime`` is spelled the way ``ps --format json`` spells it
    (``1.5h``), parsed with the same helper, so the key means one thing.
    """
    dt_created = _parse_timestamp(pod.created_at) if pod.created_at else None
    price = pod.executor.price_per_hour if pod.executor and pod.executor.price_per_hour else None
    hours = None
    if dt_created:
        hours = max(0.0, ((now or datetime.now(timezone.utc)) - dt_created).total_seconds() / 3600)
    if hours is None or price is None:
        spent = None
    elif getattr(pod, "status", None) in NOT_YET_BILLING:
        # the same rule as `lium spend`: a pod that never left PENDING has not billed
        spent = 0.0
    else:
        spent = round(hours * price, 2)

    return {
        "uptime": format_duration(hours * 3600) if hours is not None else None,
        "uptime_hours": round(hours, 2) if hours is not None else None,
        "price_per_hour": price,
        "spent_usd": spent,
        "spent_is_estimate": True,
    }


def format_removed_line(pod: PodInfo, spend: Dict[str, Any]) -> str:
    """``removed <name> — <uptime> at $X/h ≈ $Y``; ``≈`` because it is not a billed figure."""
    name = pod.name or pod.huid
    uptime = spend["uptime"] or "?"
    if spend["spent_usd"] is None:
        return f"removed {name} — {uptime} (no $/h on record)"
    return f"removed {name} — {uptime} at ${spend['price_per_hour']:.2f}/h ≈ ${spend['spent_usd']:.2f}"


def calculate_pod_cost(pod: PodInfo) -> float:
    """Calculate total cost of a pod since creation.

    Args:
        pod: Pod to calculate cost for

    Returns:
        Total cost in dollars
    """
    return pod_spend(pod)["spent_usd"] or 0.0


def format_pods_for_removal(pods: List[PodInfo], show_cost: bool = True) -> str:
    """Format pods for removal preview.

    Args:
        pods: List of pods to format
        show_cost: Whether to calculate and show total cost

    Returns:
        Formatted string ready to display
    """
    lines = ["\nPods to remove:"]
    total_cost = 0.0

    for pod in pods:
        price_info = ""
        if pod.executor and pod.executor.price_per_hour:
            price_info = f" (${pod.executor.price_per_hour:.2f}/h)"
            if show_cost:
                total_cost += calculate_pod_cost(pod)

        lines.append(f"  {pod.huid} - {pod.status}{price_info}")

    if show_cost and total_cost > 0:
        lines.append(f"\nTotal spent: ${total_cost:.2f}")

    return "\n".join(lines)


def format_pods_for_scheduled_removal(pods: List[PodInfo], termination_time: datetime) -> str:
    """Format pods for scheduled removal preview.

    Args:
        pods: List of pods to format
        termination_time: When pods will be terminated

    Returns:
        Formatted string ready to display
    """
    lines = ["\nPods to schedule for removal:"]

    for pod in pods:
        price_info = ""
        if pod.executor and pod.executor.price_per_hour:
            price_info = f" (${pod.executor.price_per_hour:.2f}/h)"
        lines.append(f"  {pod.huid} - {pod.status}{price_info}")

    # Add scheduled time info
    time_str = termination_time.strftime("%Y-%m-%d %H:%M UTC")
    now_utc = datetime.now(timezone.utc)
    time_delta = termination_time - now_utc
    hours_until = time_delta.total_seconds() / 3600

    lines.append(f"\nScheduled removal time: {time_str}")
    lines.append(f"({hours_until:.1f} hours from now)")

    return "\n".join(lines)


def format_removal_summary(success_count: int, total_count: int, failed_huids: List[str]) -> str:
    """Format removal operation summary.

    Args:
        success_count: Number of successful removals
        total_count: Total number of pods attempted
        failed_huids: List of HUIDs that failed

    Returns:
        Formatted summary string
    """
    lines = []

    if total_count > 1:
        lines.append(f"\nRemoved {success_count}/{total_count} pods")

    if failed_huids:
        lines.append(f"Failed: {', '.join(failed_huids)}")

    return "\n".join(lines) if lines else ""
