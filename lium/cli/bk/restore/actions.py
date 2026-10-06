from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class RestoreBackupAction:
    """Restore backup."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute backup restore."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        backup_id: str = ctx["backup_id"]
        restore_path: str = ctx["restore_path"]

        resolved_backup_id = lium.resolve_backup_id(backup_id)
        restore_result = lium.restore(
            pod=pod, backup_id=resolved_backup_id, restore_path=restore_path
        )

        return ActionResult(ok=True, data={"restore": restore_result})
