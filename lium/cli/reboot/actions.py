from typing import List, Tuple

from lium.cli.actions import ActionResult
from lium.sdk import Lium, LiumInsufficientBalanceError, PodInfo


class RebootPodsAction:
    """Reboot pods."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute pod reboot."""
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]
        volume_id: str | None = ctx.get("volume_id")

        rebooted = []
        failures = []
        refusals: List[Tuple[str, LiumInsufficientBalanceError]] = []

        for pod in pods:
            try:
                lium.reboot(pod, volume_id=volume_id)
                rebooted.append(pod.huid)
            except LiumInsufficientBalanceError as e:
                # a funding refusal keeps its type (exit 6, code, hint); the command raises it after the batch
                refusals.append((pod.huid, e))
            except Exception as e:
                failures.append(f"{pod.huid} ({e})")

        return ActionResult(
            ok=(len(failures) == 0 and not refusals),
            data={"rebooted": rebooted, "failures": failures, "refusals": refusals}
        )
