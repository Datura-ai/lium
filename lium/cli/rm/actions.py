from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, LiumBudgetExceededError, PodInfo
from lium.cli import ui


class RemovePodsAction:
    """Remove pods immediately."""

    def execute(self, ctx: dict) -> ActionResult:
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]

        failed_huids = []

        for pod in pods:
            try:
                lium.rm(pod)
            except Exception as e:
                ui.debug(f"Failed to remove {pod.huid}: {e}")
                failed_huids.append(pod.huid)

        return ActionResult(
            ok=(len(failed_huids) == 0),
            data={"failed_huids": failed_huids}
        )


class ScheduleRemovalAction:
    """Schedule pod removal at a future time."""

    def execute(self, ctx: dict) -> ActionResult:
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]
        termination_time: str = ctx["termination_time"]

        failed_huids = []

        for pod in pods:
            try:
                lium.schedule_termination(pod, termination_time=termination_time)
            except LiumBudgetExceededError:
                # the key's budget refused the new duration (402, lium-platform#630): it is the key, not this pod,
                # that is over — every pod after it would be refused alike, and the reader needs the server's
                # sentence (the window hit, the figures), which handle_errors prints as it does for `up`
                raise
            except Exception as e:
                ui.debug(f"Failed to schedule {pod.huid}: {e}")
                failed_huids.append(pod.huid)

        return ActionResult(
            ok=(len(failed_huids) == 0),
            data={"failed_huids": failed_huids}
        )
