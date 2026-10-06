from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class GetBackupLogsAction:
    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo | None = ctx.get("pod")
        backup_id: str | None = ctx.get("backup_id")

        if backup_id:
            resolved_id = lium.resolve_backup_id(backup_id)
            return ActionResult(
                ok=True,
                data={"single_backup": True, "log": lium.backup_log(resolved_id)},
            )

        # Get logs for specific pod
        backup_logs = lium.backup_logs(pod=pod) if pod else []

        if not backup_logs:
            return ActionResult(ok=True, data={"logs": []})

        return ActionResult(ok=True, data={"logs": backup_logs[:10]})
