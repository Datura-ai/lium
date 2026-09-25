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

        api_key_id = ctx.get("api_key_id")
        # the keyword only when filtering: the plain call is what every caller (and test double) of ps() has
        pods = lium.ps(api_key_id=api_key_id) if api_key_id else lium.ps()
        return ActionResult(
            ok=True,
            data={"pods": pods}
        )
