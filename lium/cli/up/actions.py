from typing import Callable, Dict, List, Optional
import re
import time

import paramiko

from lium.cli.actions import ActionResult
from lium.sdk import ExecutorInfo, Template, PodInfo, Lium, LiumError
from lium.sdk.client import RENT_BY_SPEC
from lium.cli.utils import (
    MIN_DOWNLOAD_MBPS,
    _api_error_data,
    calculate_pareto_frontier,
    resolve_executor_indices,
    get_pytorch_template_id,
    wait_for_pod_ready,
)


class ResolveExecutorAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor_id: Optional[str] = ctx.get("executor_id")
        gpu: Optional[str] = ctx.get("gpu")
        count: Optional[int] = ctx.get("count")
        country: Optional[str] = ctx.get("country")
        min_cpus: Optional[int] = ctx.get("min_cpus")
        ports: Optional[int] = ctx.get("ports")

        if executor_id:
            if executor_id.isdigit():
                resolved_ids, error = resolve_executor_indices([executor_id])
                if error or not resolved_ids:
                    return ActionResult(ok=False, data={}, error=error or "Failed to resolve node index")
                executor_id = resolved_ids[0]

            executor = lium.get_executor(executor_id)
            if not executor:
                return ActionResult(ok=False, data={}, error=f"Node '{executor_id}' not found")

            if ports and (not executor.available_port_count or executor.available_port_count < ports):
                available = executor.available_port_count or 0
                return ActionResult(
                    ok=False,
                    data={},
                    error=f"Node {executor.huid} has insufficient ports (available: {available}, required: {ports})"
                )
        elif gpu and lium.supports(RENT_BY_SPEC):
            # The backend picks: one dry-run call instead of listing the fleet here. The same
            # spec, capped at the price shown, rents in RentPodAction (DAH-3047).
            spec = {
                "gpu_type": gpu,
                "gpu_count": count or 1,
                "country": country,
                "min_ports": ports,
                "min_cpus": min_cpus,
                # the floor the Pareto path below has always applied
                "min_download_mbps": MIN_DOWNLOAD_MBPS,
            }
            spec = {key: value for key, value in spec.items() if value is not None}
            try:
                pick = lium.rent(
                    **spec,
                    template_id=ctx.get("template_id"),
                    dockerfile_content=ctx.get("dockerfile_content"),
                    dry_run=True,
                )
            except LiumError as exc:
                if type(exc) is not LiumError:
                    raise  # auth, permission, not-found, rate-limit and server errors keep their own codes
                # "No node matches …" (client-side) or the server's 409: the same outcome as the
                # Pareto path's empty list below — node_selection_failed, not an API error. The
                # server's hint and request_id ride along in data (DAH-3057); the command lifts
                # the hint out into the failure's own.
                data = {**(_api_error_data(exc) or {}), **({"hint": exc.hint} if exc.hint else {})}
                return ActionResult(ok=False, data=data, error=str(exc))
            return ActionResult(
                ok=True,
                data={
                    "executor": pick.executor,
                    "auto_selected": True,
                    "candidates": pick.candidates,
                    "spec": spec,
                    "price_per_hour": pick.price_per_hour,
                    # the GPUs the rental gets: a split of a larger node when the server allows one
                    "gpu_count": pick.gpu_count,
                    "template_id": pick.template_id,
                },
            )
        else:
            executors = lium.ls(gpu_type=gpu, min_cpus=min_cpus)

            if count:
                executors = [e for e in executors if e.gpu_count == count]
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
                if gpu and (known := lium.unknown_gpu_type(gpu)) is not None:
                    return ActionResult(
                        ok=False, data={},
                        error=f"No GPU type matches '{gpu}'. Types on the marketplace: {', '.join(known)}",
                    )
                filters = []
                if gpu:
                    filters.append(f"GPU type={gpu}")
                if count:
                    filters.append(f"GPU count={count}")
                if country:
                    filters.append(f"country={country}")
                if min_cpus:
                    filters.append(f"min CPUs={min_cpus}")
                if ports:
                    filters.append(f"min ports={ports}")
                filter_desc = ', '.join(filters) if filters else "specified filters"
                return ActionResult(ok=False, data={}, error=f"No nodes available with {filter_desc}")

            from lium.cli.ls.command import ls_store_executor
            ls_store_executor(gpu_type=gpu)

            pareto_flags = calculate_pareto_frontier(executors)
            pareto_executors = [e for e, is_pareto in zip(executors, pareto_flags) if is_pareto]
            candidates = pareto_executors or executors
            # Cheapest $/GPU·h of the optimal set; min() keeps the first of a
            # tie, so equal prices fall back to the listing order as before.
            executor = min(candidates, key=lambda e: e.price_per_gpu or float("inf"))
            return ActionResult(
                ok=True,
                data={"executor": executor, "auto_selected": True, "candidates": len(candidates)},
            )

        return ActionResult(ok=True, data={"executor": executor})


class ResolveTemplateAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        template_id: Optional[str] = ctx.get("template_id")
        executor: Optional[ExecutorInfo] = ctx.get("executor")

        if template_id:
            template = lium.get_template(template_id)
            if not template:
                return ActionResult(ok=False, data={}, error=f"Template '{template_id}' not found")
        else:
            template = lium.default_docker_template(executor.id) if executor else None
            if not template:
                template = lium.get_template(get_pytorch_template_id())

        return ActionResult(ok=True, data={"template": template})


class CreateEphemeralTemplateAction:
    """Create an ephemeral template for docker-run style execution."""

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        image: str = ctx["image"]
        env: Dict[str, str] = ctx.get("env", {})
        entrypoint: Optional[str] = ctx.get("entrypoint", "")
        cmd: Optional[str] = ctx.get("cmd", "")
        ports: List[int] = ctx.get("ports", [22])

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
            environment=env or {},
            is_private=True,
            one_time_template=True,
        )

        return ActionResult(ok=True, data={"template": template})


class CreateVolumeAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        volume_create_params: Dict[str, str] = ctx["volume_create_params"]

        volume = lium.volume_create(
            name=volume_create_params['name'],
            description=volume_create_params.get('description', '')
        )
        return ActionResult(ok=True, data={"volume": volume, "volume_id": volume.id})


class RentPodAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        executor: ExecutorInfo = ctx["executor"]
        template: Optional[Template] = ctx.get("template")
        dockerfile_content: Optional[str] = ctx.get("dockerfile_content")
        name: Optional[str] = ctx.get("name")
        volume_id: Optional[str] = ctx.get("volume_id")
        ports: Optional[int] = ctx.get("ports")
        ssh_name: Optional[str] = ctx.get("ssh_name")
        enable_volume_encryption: bool | None = ctx.get("enable_volume_encryption")
        backup_id: Optional[str] = ctx.get("backup_id")
        restore_path: Optional[str] = ctx.get("restore_path")

        if not name:
            name = executor.huid

        rental = dict(
            name=name,
            template_id=template.id if template else None,
            dockerfile_content=dockerfile_content,
            volume_id=volume_id,
            ports=ports,
            ssh_name=ssh_name,
            enable_volume_encryption=enable_volume_encryption,
            backup_id=backup_id,
            restore_path=restore_path,
        )
        spec: Optional[Dict] = ctx.get("spec")
        price_per_hour = getattr(executor, "price_per_hour", None)
        gpu_count = getattr(executor, "gpu_count", None)
        if spec:
            # The server re-selects at rent time, so a pick taken since the dry run falls
            # through to the next candidate — never one dearer than the price confirmed.
            result = lium.rent(**spec, max_price_per_gpu_hour=executor.price_per_gpu, **rental)
            pod_info, executor = result.pod, result.executor
            price_per_hour, gpu_count = result.price_per_hour, result.gpu_count
        else:
            pod_info = lium.up(executor_id=executor.id, **rental)

        pod_id = pod_info.get('id') or pod_info.get('name', '')
        return ActionResult(
            ok=True,
            data={
                "pod_info": pod_info,
                "pod_id": pod_id,
                "pod_name": name,
                "executor": executor,
                "price_per_hour": price_per_hour,
                "gpu_count": gpu_count,
            },
        )


class WaitReadyAction:
    """Wait for the rented pod. ``ctx["timeout"]`` (seconds) bounds the wait; None is unbounded.

    Propagates ``PodStartError`` from the SDK: a pod that FAILED or vanished is
    not a pod worth waiting for, and the caller must be told which pod it is
    still paying for. A timeout is reported as ``ok=False`` with the same intent.
    """

    # A line every this many seconds while nothing changes; every status change prints one too.
    PROGRESS_EVERY_SECONDS = 30

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod_id: str = ctx["pod_id"]
        timeout: Optional[int] = ctx.get("timeout")
        report = ctx.get("report")

        last_seen: dict = {"pod": None}
        pod = wait_for_pod_ready(
            lium, pod_id, timeout=timeout, on_poll=self._progress(report, last_seen) if report else None
        )
        if pod is None:
            error = f"Pod {pod_id} was still starting after {timeout}s"
            # DAH-3005: the backend's own estimate tells a slow-but-coming pod from a stuck one.
            # It travels in ``data`` too, so the command can put it in its own message.
            hint = last_seen["pod"].eta_hint() if last_seen["pod"] is not None else None
            if hint:
                error += f" (backend: {hint})"
            return ActionResult(ok=False, data={"eta_hint": hint}, error=error)
        return ActionResult(ok=True, data={"pod": pod})

    def _progress(
        self, report: Optional[Callable[[str], None]], last_seen: Optional[dict] = None
    ) -> Optional[Callable[[Optional[PodInfo], str, float], None]]:
        """An on_poll callback that says what the pod is doing, without repeating itself every poll.

        A silent wait is what turned a slow rent into a killed command: nothing tells the caller
        (or an agent behind a pipe, where the spinner is not drawn) whether the pod is PENDING,
        pulling an image, or already gone. DAH-3005: the line carries the backend's estimate and
        creation phase when it sends them, and is printed again whenever the phase moves.
        """
        if report is None:
            return None
        last = {"status": None, "phase": None, "at": 0.0}

        def on_poll(pod: Optional[PodInfo], status: str, elapsed: float) -> None:
            if last_seen is not None and pod is not None:
                last_seen["pod"] = pod
            phase = pod.phase if pod is not None else None
            changed = status != last["status"] or phase != last["phase"]
            if not changed and elapsed - last["at"] < self.PROGRESS_EVERY_SECONDS:
                return
            last["status"], last["phase"], last["at"] = status, phase, elapsed
            label = pod.huid if pod is not None else "pod"
            line = f"waiting for {label}… {status} ({int(elapsed)} s)"
            hint = pod.eta_hint() if pod is not None else None
            if hint:
                line += f" · {hint}"
            report(line)

        return on_poll


def rented_gpu_count(executor: ExecutorInfo) -> int:
    """The GPU count a rent with no ``--count`` gets: the node's free GPUs.

    The rent request names no count, so the backend gives the pod every GPU that
    is free on the node and bills for those. On a partially rented split host
    that is fewer than ``gpu_count``, the whole host, and comparing the pod with
    the host total would fail a correct pod. ``gpu_count`` is the fallback when
    the API did not send ``available_gpu_count``.
    """
    available = getattr(executor, "available_gpu_count", None)
    return executor.gpu_count if available is None else available


def billed_gpu_count(pod: PodInfo) -> Optional[int]:
    """The GPU count the API bills this pod for, or None when the API did not say.

    This is the pod row's own ``gpu_count`` from ``/pods``. The nested executor
    describes the whole host, so its count is the wrong side of the comparison
    for a GPU-split rental: a pod holding 2 of the host's 8 GPUs is billed for 2.
    """
    count = getattr(pod, "gpu_count", None)
    if count is None:
        return None
    try:
        return int(count)
    except (TypeError, ValueError):
        return None


VISIBLE_GPU_COUNT_COMMAND = "nvidia-smi -L"
_GPU_LINE = re.compile(r"^GPU \d+:", re.MULTILINE)

# sshd inside a pod that just turned RUNNING may not be listening yet: the first
# connection is retried for about this long before the check is given up.
SSH_RETRY_SECONDS = 90
SSH_RETRY_INTERVAL = 5
SSH_RETRY_ERRORS = (OSError, EOFError, paramiko.SSHException)


def parse_visible_gpu_count(stdout: str) -> Optional[int]:
    """How many ``GPU n:`` lines ``nvidia-smi -L`` printed, or None if it printed none."""
    return len(_GPU_LINE.findall(stdout or "")) or None


class VerifyGpuCountAction:
    """Check that the pod exposes the GPUs that were requested and billed.

    Two checks, each independent of the other:

    * billed: the count the API bills the pod for versus the count requested
      (``--count``, or the chosen node's free GPU count when ``--count`` was not
      given — ``rented_gpu_count``).
    * visible: with ``verify_via_ssh``, the count ``nvidia-smi -L`` reports inside
      the pod versus the billed count. The connection is retried while sshd comes
      up; a command that fails (``nvidia-smi`` missing, driver not loaded) is
      "could not check", never a mismatch.

    ``ok`` is False when either check found a mismatch or the SSH check could not
    run. ``data["mismatch"]`` is True only for an actual mismatch, so a caller can
    tell "the pod is wrong" from "the pod could not be checked".
    """

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        expected: Optional[int] = ctx.get("expected_count")
        executor_id: Optional[str] = ctx.get("executor_id")
        verify_via_ssh: bool = bool(ctx.get("verify_via_ssh"))
        retry_seconds: float = ctx.get("ssh_retry_seconds", SSH_RETRY_SECONDS)
        sleep = ctx.get("sleep", time.sleep)

        billed = billed_gpu_count(pod)
        visible: Optional[int] = None
        data = {
            "expected": expected,
            "billed": billed,
            "visible": visible,
            "executor_id": executor_id,
            "mismatch": False,
        }

        if expected is not None and billed is not None and billed != expected:
            data["mismatch"] = True
            return ActionResult(
                ok=False,
                data=data,
                error=(
                    f"GPU count mismatch: requested {expected}, pod is billed for {billed} "
                    f"(node {executor_id})"
                ),
            )

        if not verify_via_ssh:
            return ActionResult(ok=True, data=data)

        result = None
        last_error: Optional[BaseException] = None
        attempts = max(1, int(retry_seconds // SSH_RETRY_INTERVAL) + 1)
        for attempt in range(attempts):
            try:
                result = lium.exec(pod, command=VISIBLE_GPU_COUNT_COMMAND)
                break
            except SSH_RETRY_ERRORS as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    sleep(SSH_RETRY_INTERVAL)
            except Exception as exc:
                return ActionResult(
                    ok=False, data=data, error=f"Could not verify GPU count over SSH: {exc}"
                )
        if result is None:
            return ActionResult(
                ok=False,
                data=data,
                error=(
                    f"Could not verify GPU count over SSH: no connection after {retry_seconds:g}s "
                    f"({last_error})"
                ),
            )

        exit_code = result.get("exit_code", 0)
        if exit_code not in (0, None) or result.get("success") is False:
            detail = (result.get("stderr") or result.get("stdout") or "").strip().splitlines()
            return ActionResult(
                ok=False,
                data=data,
                error=(
                    f"Could not verify GPU count over SSH: '{VISIBLE_GPU_COUNT_COMMAND}' exited "
                    f"{exit_code}" + (f" ({detail[0].strip()})" if detail else "")
                ),
            )

        visible = parse_visible_gpu_count(str(result.get("stdout") or ""))
        data["visible"] = visible
        if visible is None:
            return ActionResult(
                ok=False,
                data=data,
                error=(
                    "Could not verify GPU count over SSH: "
                    f"'{VISIBLE_GPU_COUNT_COMMAND}' listed no GPU"
                ),
            )

        reference = billed if billed is not None else expected
        if reference is not None and visible != reference:
            data["mismatch"] = True
            return ActionResult(
                ok=False,
                data=data,
                error=(
                    f"GPU count mismatch: pod is billed for {reference}, "
                    f"nvidia-smi reports {visible} (node {executor_id})"
                ),
            )

        return ActionResult(ok=True, data=data)


class ScheduleTerminationAction:
    """Set the pod's removal time. ``ctx["pod"]`` is a PodInfo or the pod id: `up` runs this
    right after the rent, before the pod is listed as ready."""

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo | str = ctx["pod"]
        termination_time = ctx["termination_time"]

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


class InstallJupyterAction:

    def execute(self, ctx: dict) -> ActionResult:
        lium: Lium = ctx["lium"]
        pod: PodInfo = ctx["pod"]
        ui = ctx.get("ui")

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


class PrepareSSHAction:

    def execute(self, ctx: dict) -> ActionResult:
        pod_name: str = ctx["pod_name"]

        from lium.cli.ssh.command import get_ssh_method_and_pod
        ssh_argv, pod = get_ssh_method_and_pod(pod_name)
        return ActionResult(ok=True, data={"ssh_argv": ssh_argv, "pod": pod})
