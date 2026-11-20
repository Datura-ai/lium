from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, LiumBudgetExceededError, LiumInsufficientBalanceError, PodInfo


class CancelSchedulesAction:
    """Cancel scheduled terminations."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute schedule cancellations."""
        pods: List[PodInfo] = ctx["pods"]
        lium: Lium = ctx["lium"]

        failures = []
        refusals: List[LiumBudgetExceededError | LiumInsufficientBalanceError] = []
        cancelled = []

        for pod in pods:
            try:
                lium.cancel_scheduled_termination(pod)
                cancelled.append(pod.huid)
            except (LiumBudgetExceededError, LiumInsufficientBalanceError) as e:
                # a 402 (the key's budget, or no balance) keeps its type (exit 6, code, hint); raised after the batch
                refusals.append(e)
            except Exception as e:
                failures.append(f"{pod.huid} ({e})")

        return ActionResult(
            ok=(len(failures) == 0 and not refusals),
            data={"failures": failures, "refusals": refusals, "cancelled": cancelled}
        )
