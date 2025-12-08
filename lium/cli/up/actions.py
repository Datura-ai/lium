from typing import Optional, Dict, List
import time

from lium.cli.actions import ActionResult
from lium.sdk import ExecutorInfo, Template, PodInfo, Lium
from lium.cli.utils import (
    calculate_pareto_frontier,
    resolve_executor_indices,
    get_pytorch_template_id,
    wait_ready_no_timeout,
)


class ResolveExecutorAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor_id: Optional[str] = ctx.get("executor_id")
        gpu: Optional[str] = ctx.get("gpu")
        count: Optional[int] = ctx.get("count")
        country: Optional[str] = ctx.get("country")
        ports: Optional[int] = ctx.get("ports")

        try:
            if executor_id:
                if executor_id.isdigit():
                    resolved_ids, error = resolve_executor_indices([executor_id])
                    if error or not resolved_ids:
                        return ActionResult(ok=False, data={}, error=error or "Failed to resolve executor index")
                    executor_id = resolved_ids[0]

                executor = lium.get_executor(executor_id)
                if not executor:
                    return ActionResult(ok=False, data={}, error=f"Executor '{executor_id}' not found")
                
                if count and count > executor.available_gpu_count:
                    return ActionResult(ok=False, data={}, error=f"Executor {executor.huid} has insufficient GPUs (available: {executor.available_gpu_count}, required: {count})")

                if ports and (not executor.available_port_count or executor.available_port_count < ports):
                    available = executor.available_port_count or 0
                    return ActionResult(
                        ok=False,
                        data={},
                        error=f"Executor {executor.huid} has insufficient ports (available: {available}, required: {ports})"
                    )
            else:
                executors = lium.ls(gpu_type=gpu)

                if count:
                    executors = [e for e in executors if e.available_gpu_count >= count]
                if country:
                    executors = [
                        e for e in executors
                        if e.location and e.location.get('country_code', '').upper() == country.upper()
                    ]
                if ports:
                    executors = [
                        e for e in executors
                        if e.available_port_count and e.available_port_count >= ports
                    ]

                if not executors:
                    filters = []
                    if gpu:
                        filters.append(f"GPU type={gpu}")
                    if count:
                        filters.append(f"GPU count={count}")
                    if country:
                        filters.append(f"country={country}")
                    if ports:
                        filters.append(f"min ports={ports}")
                    filter_desc = ', '.join(filters) if filters else "specified filters"
                    return ActionResult(ok=False, data={}, error=f"No executors available with {filter_desc}")

                from lium.cli.ls.command import ls_store_executor
                ls_store_executor(gpu_type=gpu)

                pareto_flags = calculate_pareto_frontier(executors)
                pareto_executors = [e for e, is_pareto in zip(executors, pareto_flags) if is_pareto]
                executor = pareto_executors[0] if pareto_executors else executors[0]

            return ActionResult(ok=True, data={"executor": executor})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class ResolveTemplateAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        template_id: Optional[str] = ctx.get("template_id")
        executor: Optional[ExecutorInfo] = ctx.get("executor")

        try:
            if template_id:
                template = lium.get_template(template_id)
                if not template:
                    return ActionResult(ok=False, data={}, error=f"Template '{template_id}' not found")
            else:
                template = lium.default_docker_template(executor.id) if executor else None
                if not template:
                    template = lium.get_template(get_pytorch_template_id())

            return ActionResult(ok=True, data={"template": template})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class CreateEphemeralTemplateAction:
    """Create an ephemeral template for docker-run style execution."""

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        image: str = ctx["image"]
        env: Dict[str, str] = ctx.get("env", {})
        entrypoint: Optional[str] = ctx.get("entrypoint", "")
        cmd: Optional[str] = ctx.get("cmd", "")
        ports: List[int] = ctx.get("ports", [22])

        try:
            # Parse image:tag
            if ":" in image:
                docker_image, docker_tag = image.rsplit(":", 1)
            else:
                docker_image = image
                docker_tag = "latest"

            # Generate a unique name for the ephemeral template
            import hashlib
            hash_input = f"{image}{env}{entrypoint}{cmd}"
            short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]
            template_name = f"ephemeral-{short_hash}"

            template = lium.create_template(
                name=template_name,
                docker_image=docker_image,
                docker_image_tag=docker_tag,
                ports=ports,
                start_command=cmd,
                entrypoint=entrypoint,
                environment=env if env else None,
                is_private=True,
            )

            return ActionResult(ok=True, data={"template": template})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class CreateVolumeAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        volume_create_params: Dict[str, str] = ctx["volume_create_params"]

        try:
            volume = lium.volume_create(
                name=volume_create_params['name'],
                description=volume_create_params.get('description', '')
            )
            return ActionResult(ok=True, data={"volume": volume, "volume_id": volume.id})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class RentPodAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor: ExecutorInfo = ctx["executor"]
        template: Template = ctx["template"]
        gpu_count: Optional[int] = ctx.get("gpu_count")
        name: Optional[str] = ctx.get("name")
        volume_id: Optional[str] = ctx.get("volume_id")
        ports: Optional[int] = ctx.get("ports")

        try:
            if not name:
                name = executor.huid

            pod_info = lium.up(
                executor_id=executor.id,
                name=name,
                gpu_count=gpu_count,
                template_id=template.id if template else None,
                volume_id=volume_id,
                ports=ports,
            )

            pod_id = pod_info.get('id') or pod_info.get('name', '')
            return ActionResult(ok=True, data={"pod_info": pod_info, "pod_id": pod_id, "pod_name": name})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class WaitReadyAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod_id: str = ctx["pod_id"]

        try:
            pod = wait_ready_no_timeout(lium, pod_id)
            return ActionResult(ok=True, data={"pod": pod})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class ScheduleTerminationAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        termination_time = ctx["termination_time"]

        try:
            termination_time_str = termination_time.isoformat()
            lium.schedule_termination(pod, termination_time=termination_time_str)

            from datetime import datetime, timezone
            time_delta = termination_time - datetime.now(timezone.utc)
            hours_until = time_delta.total_seconds() / 3600

            return ActionResult(
                ok=True,
                data={
                    "termination_time": termination_time,
                    "hours_until": hours_until
                }
            )

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class InstallJupyterAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        ui = ctx.get("ui")

        try:
            if not pod.ports:
                return ActionResult(ok=False, data={}, error="No ports allocated to pod for Jupyter installation")

            available_ports = [int(port) for port in pod.ports.keys() if int(port) != 22]

            if not available_ports:
                return ActionResult(
                    ok=False,
                    data={},
                    error="No suitable ports available for Jupyter (only SSH port 22 found)"
                )

            jupyter_port = available_ports[0]

            lium.install_jupyter(pod, jupyter_internal_port=jupyter_port)

            max_wait = 120
            wait_interval = 3
            elapsed = 0
            pod_id = pod.id

            while elapsed < max_wait:
                time.sleep(wait_interval)
                elapsed += wait_interval

                all_pods = lium.ps()
                updated_pod = next((p for p in all_pods if p.id == pod_id or p.huid == pod_id or p.name == pod_id), None)

                if updated_pod and hasattr(updated_pod, 'jupyter_installation_status'):
                    if updated_pod.jupyter_installation_status == "SUCCESS":
                        jupyter_url = getattr(updated_pod, 'jupyter_url', None)
                        return ActionResult(
                            ok=True,
                            data={"jupyter_url": jupyter_url, "jupyter_port": jupyter_port}
                        )
                    elif updated_pod.jupyter_installation_status == "FAILED":
                        error_details = getattr(updated_pod, 'jupyter_error', '')
                        error_msg = f"Jupyter installation failed"
                        if error_details:
                            error_msg += f": {error_details}"
                        return ActionResult(ok=False, data={}, error=error_msg)

            return ActionResult(
                ok=False,
                data={},
                error="Jupyter installation timed out. Run 'lium ps' to check status"
            )

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))


class PrepareSSHAction:

    def execute(self, ctx: dict) -> ActionResult:
        pod_name: str = ctx["pod_name"]

        try:
            from lium.cli.ssh.command import get_ssh_method_and_pod
            ssh_cmd, pod = get_ssh_method_and_pod(pod_name)
            return ActionResult(ok=True, data={"ssh_cmd": ssh_cmd, "pod": pod})

        except Exception as e:
            return ActionResult(ok=False, data={}, error=str(e))
