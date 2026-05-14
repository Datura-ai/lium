from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class SetBackupAction:
    """Set backup configuration."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute backup set."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        path: str = ctx["path"]
        frequency_hours: int = ctx["frequency_hours"]
        retention_days: int = ctx["retention_days"]

        try:
            # Check if backup already exists
            existing_config = lium.backup_config(pod)

            if existing_config:
                lium.backup_delete(existing_config.id)

            # Create new backup config
            backup_config = lium.backup_create(
                pod=pod,
                path=path,
                frequency_hours=frequency_hours,
                retention_days=retention_days
            )

            return ActionResult(ok=True, data={"backup_config": backup_config})
        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
