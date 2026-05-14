from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class RemoveBackupAction:
    """Remove backup configuration."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute backup removal."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]

        try:
            backup_config = lium.backup_config(pod)

            if not backup_config:
                return ActionResult(ok=False, data={}, error="No backup configuration found")

            lium.backup_delete(backup_config.id)

            return ActionResult(ok=True, data={"backup_config_id": backup_config.id})
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
