from typing import List

from lium.cli.actions import ActionResult


class GetExecutorsAction:
    """Get available executors."""

    def execute(self, ctx: dict) -> ActionResult:
        """Get executors list.

        """
        lium = ctx["lium"]
        gpu_type = ctx.get("gpu_type")
        gpu_count = ctx.get("gpu_count")
        lat = ctx.get("lat")
        lon = ctx.get("lon")
        max_distance = ctx.get("max_distance")
        min_cuda_version = ctx.get("min_cuda_version")

        try:
            executors = lium.ls(
                gpu_type=gpu_type,
                gpu_count=gpu_count,
                lat=lat,
                lon=lon,
                max_distance_miles=max_distance,
                min_cuda_version=min_cuda_version,
            )
            return ActionResult(
                ok=True,
                data={"executors": executors}
            )
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
