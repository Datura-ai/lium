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
        min_cpus = ctx.get("min_cpus")
        nvlink = ctx.get("nvlink") or None
        min_download_mbps = ctx.get("min_download_mbps")

        executors = lium.ls(
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            lat=lat,
            lon=lon,
            max_distance_miles=max_distance,
            min_cuda_version=min_cuda_version,
            min_cpus=min_cpus,
            nvlink=nvlink,
            min_download_mbps=min_download_mbps,
        )
        return ActionResult(
            ok=True,
            data={"executors": executors}
        )
