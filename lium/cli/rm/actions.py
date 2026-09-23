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
        budget_errors: List[LiumBudgetExceededError] = []

        for pod in pods:
            try:
                lium.schedule_termination(pod, termination_time=termination_time)
            except LiumBudgetExceededError as e:
                # The server refuses only this pod's extension. Collect the 402 and keep
                # scheduling the rest so `rm a b --in 2h` still sets b when a is refused.
                budget_errors.append(e)
            except Exception as e:
                ui.debug(f"Failed to schedule {pod.huid}: {e}")
                failed_huids.append(pod.huid)

        if budget_errors:
            raise budget_errors[0]

        return ActionResult(
            ok=(len(failed_huids) == 0),
            data={"failed_huids": failed_huids}
        )
