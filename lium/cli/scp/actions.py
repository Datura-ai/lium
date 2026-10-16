from pathlib import Path
from typing import List

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo
from lium.cli import ui


class ScpAction:
    """Execute SCP operations."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute SCP upload or download."""
        lium: Lium = ctx["lium"]
        pods: List[PodInfo] = ctx["pods"]
        download: bool = ctx["download"]

        failed_huids = []
        directory_error = None

        if not download:
            local_file: Path = ctx["local_file"]
            remote_path: str = ctx["remote_path"]

            for pod in pods:
                try:
                    lium.scp(pod, local=str(local_file), remote=remote_path)
                except Exception as e:
                    ui.debug(f"Failed to copy to {pod.huid}: {e}")
                    failed_huids.append(pod.huid)
        else:
            remote_path: str = ctx["remote_path"]
            destination_map: dict = ctx["destination_map"]

            for pod in pods:
                try:
                    _, local_dest = destination_map[pod.huid]
                    local_dest.parent.mkdir(parents=True, exist_ok=True)
                    lium.download(pod, remote=remote_path, local=str(local_dest))
                except IsADirectoryError as e:
                    ui.debug(f"Failed to download from {pod.huid}: {e}")
                    failed_huids.append(pod.huid)
                    directory_error = str(e)
                except Exception as e:
                    ui.debug(f"Failed to download from {pod.huid}: {e}")
                    failed_huids.append(pod.huid)

        return ActionResult(
            ok=(len(failed_huids) == 0),
            data={"failed_huids": failed_huids, "directory_error": directory_error}
        )
