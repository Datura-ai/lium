from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class CancelSchedulesAction:
    """Cancel scheduled terminations."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute schedule cancellations."""
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]

        failures = []

        for pod in pods:
            try:
                lium.cancel_scheduled_termination(pod)
            except Exception as e:
                failures.append(f"{pod.huid} ({e})")

        return ActionResult(
            ok=(len(failures) == 0),
            data={"failures": failures}
        )
