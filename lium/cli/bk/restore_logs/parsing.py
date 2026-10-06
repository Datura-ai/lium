"""Parsing logic for bk restore-logs command."""

from typing import List

from lium.cli.utils import parse_targets
from lium.sdk import PodInfo


def parse(pod_id: str | None, all_pods: List[PodInfo]) -> tuple[dict | None, str]:
    """Parse bk restore-logs arguments."""

    if not pod_id:
        return {"pod_name": None}, ""

    selected_pods = parse_targets(pod_id, all_pods)

    if not selected_pods:
        return None, f"Pod '{pod_id}' not found"

    pod = selected_pods[0]
    pod_name = pod.name or pod.huid

    return {"pod": pod, "pod_name": pod_name}, ""
