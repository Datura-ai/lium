from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class RebootPodsAction:
    """Reboot pods."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute pod reboot."""
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]
        volume_id: str | None = ctx.get("volume_id")

        failures = []

        for pod in pods:
            try:
                lium.reboot(pod, volume_id=volume_id)
            except Exception as e:
                failures.append(f"{pod.huid} ({e})")

        return ActionResult(
            ok=(len(failures) == 0),
            data={"failures": failures}
        )
