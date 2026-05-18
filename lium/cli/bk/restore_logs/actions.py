from lium.cli.actions import ActionResult
from lium.sdk import Lium, PodInfo


class GetRestoreLogsAction:
    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo | None = ctx.get("pod")
        restore_id: str | None = ctx.get("restore_id")

        try:
            if restore_id:
                all_pods = lium.ps()
                for pod_info in all_pods:
                    pod_name_search = pod_info.name or pod_info.huid
                    logs = lium.restore_logs(pod=pod_info)
                    for log in logs:
                        if getattr(log, "id", "").startswith(restore_id):
                            return ActionResult(
                                ok=True,
                                data={
                                    "single_restore": True,
                                    "pod_name": pod_name_search,
                                    "log": log,
                                },
                            )
                return ActionResult(ok=False, error=f"Restore '{restore_id}' not found", data={})

            restore_logs = lium.restore_logs(pod=pod) if pod else []

            if not restore_logs:
                return ActionResult(ok=True, data={"logs": []})

            return ActionResult(ok=True, data={"logs": restore_logs[:10]})
        except Exception as e:
            return ActionResult(ok=False, error=str(e), data={})
