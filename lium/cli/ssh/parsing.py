"""Parsing logic for ssh command."""

from typing import List

from lium.sdk import PodInfo
from lium.cli.utils import CliFailure, EXIT_POD_NOT_FOUND, EXIT_SSH_ERROR, parse_targets


def parse(target: str, all_pods: List[PodInfo]) -> tuple[dict | None, CliFailure | None]:
    """Parse ssh command arguments.

    The failure carries its own exit code, so the command layer does not have to
    read it back out of the message text: a name that matches nothing is a miss,
    a pod that exists but cannot take a session is ssh's own failure.
    """

    pods = parse_targets(target, all_pods)
    pod = pods[0] if pods else None

    if not pod:
        return None, CliFailure(
            "pod_not_found", f"Pod '{target}' not found", EXIT_POD_NOT_FOUND
        )

    if pod.status not in ["RUNNING", "STARTING"]:
        if pod.status in ["STOPPED", "FAILED"]:
            return None, CliFailure(
                "ssh_unavailable",
                f"Cannot SSH to a stopped or failed pod '{pod.huid}'",
                EXIT_SSH_ERROR,
            )
        return None, CliFailure(
            "ssh_unavailable", f"Pod '{pod.huid}' is {pod.status}", EXIT_SSH_ERROR
        )

    if not pod.ssh_cmd:
        return None, CliFailure(
            "ssh_unavailable",
            f"No SSH connection available for pod '{pod.huid}'",
            EXIT_SSH_ERROR,
        )

    return {"pod": pod}, None
