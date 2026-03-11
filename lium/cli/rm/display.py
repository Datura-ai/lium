"""Display formatting logic for rm command."""

from datetime import datetime, timezone
from typing import List

from lium.sdk import PodInfo


def calculate_pod_cost(pod: PodInfo) -> float:
    """Calculate total cost of a pod since creation.

    Args:
        pod: Pod to calculate cost for

    Returns:
        Total cost in dollars
    """
    if not pod.price or not pod.created_at:
        return 0.0

    try:
        if pod.created_at.endswith('Z'):
            dt_created = datetime.fromisoformat(pod.created_at.replace('Z', '+00:00'))
        else:
            dt_created = datetime.fromisoformat(pod.created_at)
            if not dt_created.tzinfo:
                dt_created = dt_created.replace(tzinfo=timezone.utc)

        now_utc = datetime.now(timezone.utc)
        hours = (now_utc - dt_created).total_seconds() / 3600
        return hours * pod.price
    except Exception:
        return 0.0


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
        if pod.price:
            price_info = f" (${pod.price:.2f}/h)"
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
        if pod.price:
            price_info = f" (${pod.price:.2f}/h)"
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
