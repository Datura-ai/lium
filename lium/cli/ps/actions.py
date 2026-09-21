from typing import List

from lium.cli.actions import ActionResult


class GetPodsAction:
    """Get active pods."""

    def execute(self, ctx: dict) -> ActionResult:
        """Get pods list.

        Context:
            lium: Lium SDK instance
            api_key_id: optional — only the pods rented through this API key (GET /pods?api_key_id=…)
        """
        lium = ctx["lium"]

        pods = lium.ps(api_key_id=ctx.get("api_key_id"))
        return ActionResult(
            ok=True,
            data={"pods": pods}
        )
