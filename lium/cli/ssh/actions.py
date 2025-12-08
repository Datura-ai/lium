import subprocess

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo
from lium.cli import ui


class SshAction:
    """Execute SSH connection."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute SSH to pod."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]

        try:
            ssh_cmd = lium.ssh(pod)
        except ValueError:
            ssh_cmd = pod.ssh_cmd

        ssh_cmd += " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"

        try:
            result = subprocess.run(ssh_cmd, shell=True, check=False)

            if result.returncode != 0 and result.returncode != 255:
                return ActionResult(
                    ok=False,
                    data={"exit_code": result.returncode}
                )

            return ActionResult(ok=True, data={})

        except KeyboardInterrupt:
            return ActionResult(ok=False, data={}, error="SSH session interrupted")
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
