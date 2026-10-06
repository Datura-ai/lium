from datetime import datetime, timedelta

from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class ShowBackupAction:
    """Show backup configuration."""

    def execute(self, ctx: dict) -> ActionResult:
        """Execute backup show."""
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]

        backup_config = lium.backup_config(pod)

        if not backup_config:
            return ActionResult(ok=True, data={"has_config": False})

        backup_logs = lium.backup_logs(pod)

        last_backup_info = None
        if backup_logs and len(backup_logs) > 0:
            last_log = backup_logs[0]
            status = getattr(last_log, 'status', 'UNKNOWN').upper()
            timestamp = getattr(last_log, 'created_at', 'Unknown')
            backup_id = getattr(last_log, 'id', '')[:8] if getattr(last_log, 'id', None) else 'unknown'
            last_backup_info = {
                "status": status,
                "timestamp": timestamp,
                "id": backup_id
            }

        next_due = None
        if hasattr(backup_config, 'next_backup_at') and backup_config.next_backup_at:
            next_due = backup_config.next_backup_at
        elif backup_logs and len(backup_logs) > 0:
            try:
                last_time = datetime.fromisoformat(getattr(backup_logs[0], 'created_at', '').replace('Z', '+00:00'))
                next_time = last_time + timedelta(hours=backup_config.backup_frequency_hours)
                next_due = next_time.strftime('%Y-%m-%d %H:%M')
            except:
                pass

        return ActionResult(
            ok=True,
            data={
                "has_config": True,
                "backup_path": backup_config.backup_path,
                "frequency_hours": backup_config.backup_frequency_hours,
                "retention_days": backup_config.retention_days,
                "last_backup": last_backup_info,
                "next_due": next_due
            }
        )
