"""Parsing logic for logs command."""

from typing import List

from lium.sdk import PodInfo


def parse(
    pod_id: str,
    all_pods: List[PodInfo],
) -> tuple[dict | None, str | None]:
    """Parse logs command inputs, returns (parsed_data_dict, error_message)."""
    if not all_pods:
        return None, "No active pods"

    # Find pod by id, huid, or name
    pod = next(
        (p for p in all_pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id),
        None
    )

    if not pod:
        return None, f"Pod '{pod_id}' not found"

    return {"pod": pod}, None
