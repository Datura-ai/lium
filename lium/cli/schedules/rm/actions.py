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
        budget_errors: List[LiumBudgetExceededError] = []
        budget_huids: List[str] = []
        funding_errors: List[LiumInsufficientBalanceError] = []
        funding_huids: List[str] = []
        cancelled = []

        for pod in pods:
            try:
                lium.cancel_scheduled_termination(pod)
                cancelled.append(pod.huid)
            except (LiumBudgetExceededError, LiumInsufficientBalanceError) as e:
                # a 402 keeps its type (exit 6, code, hint), collected by kind as in ScheduleRemovalAction: the key's
                # budget and an account with no balance have different fixes. Raised after the batch.
                budget = isinstance(e, LiumBudgetExceededError)
                (budget_errors if budget else funding_errors).append(e)
                (budget_huids if budget else funding_huids).append(pod.huid)
            except Exception as e:
                failures.append(f"{pod.huid} ({e})")

        return ActionResult(
            ok=(len(failures) == 0 and not budget_errors and not funding_errors),
            data={
                "failures": failures,
                "cancelled": cancelled,
                "budget_errors": budget_errors,
                "budget_huids": budget_huids,
                "funding_errors": funding_errors,
                "funding_huids": funding_huids,
            },
        )
