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

        existing_config = lium.backup_config(pod)
        if existing_config:
            # the server holds one config per pod; PUT keeps the old one if it refuses the new values
            backup_config = lium.backup_update(
                existing_config.id,
                path=path,
                frequency_hours=frequency_hours,
                retention_days=retention_days,
            )
        else:
            backup_config = lium.backup_create(
                pod=pod,
                path=path,
                frequency_hours=frequency_hours,
                retention_days=retention_days,
            )

        return ActionResult(ok=True, data={"backup_config": backup_config})
