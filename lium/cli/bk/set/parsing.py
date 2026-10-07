"""Parsing logic for bk set command."""

import re
from typing import List

from lium.sdk import PodInfo
from lium.cli.utils import parse_targets
from lium.cli.settings import config


def parse(
    pod_id: str,
    path: str,
    every: str | None,
    keep: str | None,
    all_pods: List[PodInfo]
) -> tuple[dict | None, str]:
    """Parse bk set arguments."""

    selected_pods = parse_targets(pod_id, all_pods)

    if not selected_pods:
        return None, f"Pod '{pod_id}' not found"

    pod = selected_pods[0]
    pod_name = pod.name or pod.huid

    # Parse frequency
    if every:
        match = re.fullmatch(r'(\d+)([hd])', every)
        frequency_hours = int(match.group(1))
        if match.group(2) == 'd':
            frequency_hours *= 24
    else:
        frequency_hours = config.default_backup_frequency

    # Parse retention
    if keep:
        match = re.fullmatch(r'(\d+)d', keep)
        retention_days = int(match.group(1))
    else:
        retention_days = config.default_backup_retention

    return {
        "pod": pod,
        "pod_name": pod_name,
        "path": path,
        "frequency_hours": frequency_hours,
        "retention_days": retention_days
    }, ""
