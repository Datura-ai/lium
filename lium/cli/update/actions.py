import time

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo
from lium.cli import ui


class InstallJupyterAction:
    """Install Jupyter on a pod."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute Jupyter installation."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        port: int = ctx["port"]

        lium.install_jupyter(pod, jupyter_internal_port=port)

        max_wait = 120
        wait_interval = 3
        elapsed = 0

        while elapsed < max_wait:
            time.sleep(wait_interval)
            elapsed += wait_interval

            all_pods = lium.ps()
            updated_pod = next((p for p in all_pods if p.id == pod.id), None)

            if updated_pod and hasattr(updated_pod, 'jupyter_installation_status'):
                if updated_pod.jupyter_installation_status == "SUCCESS":
                    jupyter_url = getattr(updated_pod, 'jupyter_url', None)
                    return ActionResult(
                        ok=True,
                        data={"jupyter_url": jupyter_url}
                    )
                elif updated_pod.jupyter_installation_status == "FAILED":
                    return ActionResult(ok=False, data={}, error="Jupyter installation failed")

        return ActionResult(ok=False, data={}, error="Jupyter installation timed out")
