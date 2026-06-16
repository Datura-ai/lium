from typing import Optional, Tuple
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from lium.cli.completion import get_gpu_completions
from . import validation, parsing
from .actions import (
    ResolveExecutorAction,
    ResolveTemplateAction,
    CreateEphemeralTemplateAction,
    CreateVolumeAction,
    RentPodAction,
    WaitReadyAction,
    ScheduleTerminationAction,
    InstallJupyterAction,
    PrepareSSHAction,
)


@click.command("up")
@click.argument("executor_id", required=False, metavar="NODE_ID")
@click.option("--name", "-n", help="Custom pod name")
@click.option("--template_id", "-t", help="Template ID")
@click.option("--volume", "-v", help="Volume spec: 'id:<HUID>' or 'new:name=<NAME>[,desc=<DESC>]'")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--gpu", help="Filter nodes by GPU type (e.g., H200, A6000)", shell_complete=get_gpu_completions)
@click.option("--count", "-c", type=int, help="Number of GPUs per pod")
@click.option("--country", help="Filter nodes by ISO country code (e.g., US, FR)")
@click.option("--ports", "-p", type=int, help="Minimum number of available ports required")
@click.option("--ttl", help="Auto-terminate after duration (e.g., 6h, 45m, 2d)")
@click.option("--until", help="Auto-terminate at time in local timezone (e.g., 'today 23:00', 'tomorrow 01:00', '2025-10-20 15:30')")
@click.option("--jupyter", is_flag=True, help="Install Jupyter Notebook (automatically selects available port)")
@click.option("--image", help="Docker image to run (e.g., pytorch/pytorch:2.0, nvidia/cuda:12.0)")
@click.option("--internal-ports", help="Internal ports to expose (comma-separated, e.g., 22,8000,8080)")
@click.option("--dockerfile", type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to a Dockerfile to build the pod image from (custom build; mutually exclusive with --image/--template_id)")
@click.option("-e", "--env", multiple=True, help="Environment variables (KEY=VALUE), can be repeated")
@click.option("--entrypoint", default="", help="Container entrypoint")
@click.option("--cmd", default="", help="Command to run in the container")
@click.option("--ssh-name", default=None, help="Name to register a new SSH key under (default: cli-<user>@<hostname>)")
@handle_errors
def up_command(
    executor_id: Optional[str],
    name: Optional[str],
    template_id: Optional[str],
    volume: Optional[str],
    yes: bool,
    gpu: Optional[str],
    count: Optional[int],
    country: Optional[str],
    ports: Optional[int],
    ttl: Optional[str],
    until: Optional[str],
    jupyter: bool,
    image: Optional[str],
    internal_ports: Optional[str],
    dockerfile: Optional[str],
    env: Tuple[str, ...],
    entrypoint: Optional[str],
    cmd: Optional[str],
    ssh_name: Optional[str],
):
    """\b
    Create a new GPU pod on a node.
    \b
    NODE_ID: Node UUID, HUID, or index from last 'lium ls'.
    If not provided, uses filters to auto-select best node.
    \b
    Examples:
      lium up cosmic-hawk-f2                # Create pod on specific node
      lium up 1                             # Create pod on node #1 from last ls
      lium up --gpu H200                    # Auto-select best H200 node
      lium up --gpu A6000 -c 2              # Auto-select best 2×A6000 node
      lium up --country US                  # Auto-select best node in US
      lium up --gpu H200 --country FR       # Combine multiple filters
      lium up --ports 5                     # Auto-select with minimum 5 ports
      lium up 1 --name my-pod               # Create with custom name
      lium up 1 --volume id:brave-fox-3a    # Attach existing volume by HUID
      lium up 1 --volume new:name=my-data   # Create and attach new volume
      lium up 1 --volume new:name=my-data,desc="Training data"  # With description
      lium up 1 --ttl 6h                    # Auto-terminate after 6 hours
      lium up 1 --until "today 23:00"       # Auto-terminate at 23:00 local time today
      lium up 1 --until "tomorrow 01:00"    # Auto-terminate at 01:00 local time tomorrow
      lium up 1 --jupyter                   # Install Jupyter Notebook (auto-selects port)
      LIUM_DEBUG=1 lium up 1 --jupyter      # Show debug information
    \b
    Docker-run style (streams logs instead of SSH):
      lium up --gpu A4000 --image pytorch/pytorch:2.0
      lium up --gpu H100 --image vllm/vllm-openai:latest -e HF_TOKEN=xxx
      lium up --gpu A6000 --image python:3.11 --cmd "python -c 'print(1+1)'"
      lium up --gpu A4000 --image myimg --entrypoint /bin/sh --cmd "-c 'echo hi'"
      lium up --gpu A4000 --image myimg --internal-ports 22,8000,8080
    \b
    Custom Dockerfile build (image built remotely from your Dockerfile):
      lium up --gpu A4000 --dockerfile ./Dockerfile
      lium up cosmic-hawk-f2 --dockerfile ./Dockerfile --name my-build
    """
    ensure_config()

    # Check if we're in docker-run mode or custom-Dockerfile build mode
    docker_run_mode = image is not None
    dockerfile_mode = dockerfile is not None

    valid, error = validation.validate(
        executor_id, gpu, count, country, ttl, until, image, template_id, dockerfile
    )
    if not valid:
        ui.error(error)
        return

    # Parse env vars if provided
    env_dict = {}
    if env:
        env_dict, error = validation.parse_env_vars(env)
        if error:
            ui.error(error)
            return

    parsed, error = parsing.parse(ttl, until, volume)
    if error:
        ui.error(error)
        return

    termination_time = parsed.get("termination_time")
    volume_id = parsed.get("volume_id")
    volume_create_params = parsed.get("volume_create_params")

    # Custom-Dockerfile build: read the Dockerfile text the CLI will send to the
    # backend (the image is built remotely; no build context is uploaded).
    dockerfile_content = None
    if dockerfile_mode:
        from pathlib import Path

        # These flags only apply to template/--image mode. In a custom build the
        # Dockerfile itself defines the image's env/entrypoint/command/ports, so
        # reject them explicitly rather than silently dropping them.
        unsupported = [
            flag
            for flag, supplied in (
                ("--env", bool(env)),
                ("--entrypoint", bool(entrypoint)),
                ("--cmd", bool(cmd)),
                ("--internal-ports", bool(internal_ports)),
            )
            if supplied
        ]
        if unsupported:
            ui.error(
                f"{', '.join(unsupported)} cannot be combined with --dockerfile "
                "(the Dockerfile defines the image's env, entrypoint, command, and ports)"
            )
            return

        try:
            dockerfile_content = Path(dockerfile).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            ui.error(f"Could not read Dockerfile: {exc}")
            return
        if not dockerfile_content.strip():
            ui.error("Dockerfile is empty")
            return
        max_bytes = 64 * 1024
        size_bytes = len(dockerfile_content.encode("utf-8"))
        if size_bytes > max_bytes:
            ui.error(
                f"Dockerfile is too large ({size_bytes} bytes); max is {max_bytes} bytes (64 KiB)"
            )
            return

    lium = Lium(source="cli")

    action = ResolveExecutorAction()
    result = ui.load(
        "Finding node",
        lambda: action.execute({
            "lium": lium,
            "executor_id": executor_id,
            "gpu": gpu,
            "count": count,
            "country": country,
            "ports": ports
        })
    )

    if not result.ok:
        ui.error(result.error)
        return

    executor = result.data["executor"]

    def _show_estimate(est_secs, dl_speed, img_gb, is_slow, warning_msg):
        est_min, est_sec = divmod(est_secs, 60)
        est_str = f"{est_min}m {est_sec}s" if est_min else f"{est_sec}s"
        img_str = f"image: ~{img_gb:.1f} GB, " if img_gb is not None else ""
        ui.dim(f"Est. deploy time: ~{est_str} ({img_str}download: {int(dl_speed)} Mbps)")
        if is_slow and warning_msg:
            ui.warning(f"Warning: {warning_msg}")

    # Resolve or create template (skipped for custom Dockerfile builds, which are
    # built remotely from the supplied Dockerfile and use no template).
    template = None
    if dockerfile_mode:
        pass
    elif docker_run_mode:
        # Parse internal ports (default to [22] if not specified)
        ports_list = [22]
        if internal_ports:
            try:
                ports_list = [int(p.strip()) for p in internal_ports.split(",")]
                # Ensure port 22 is included for SSH access
                if 22 not in ports_list:
                    ports_list.insert(0, 22)
            except ValueError:
                ui.error("Invalid port format. Use comma-separated integers (e.g., 22,8000,8080)")
                return

        action = CreateEphemeralTemplateAction()
        result = ui.load(
            "Creating template",
            lambda: action.execute({
                "lium": lium,
                "image": image,
                "env": env_dict,
                "entrypoint": entrypoint,
                "cmd": cmd,
                "ports": ports_list,
            })
        )
        if not result.ok:
            ui.error(result.error)
            return
        template = result.data["template"]
    else:
        action = ResolveTemplateAction()
        result = action.execute({
            "lium": lium,
            "template_id": template_id,
            "executor": executor
        })
        if not result.ok:
            ui.error(result.error)
            return
        template = result.data["template"]
        # API-based estimate using resolved template ID
        try:
            estimate = lium.get_deployment_estimate(executor.id, template.id)
            est_secs = estimate.get("estimated_seconds")
            if est_secs:
                dl_speed = executor.download_speed
                raw_bytes = estimate.get("docker_image_size")
                img_gb = raw_bytes / 1e9 if raw_bytes is not None else None
                _show_estimate(
                    est_secs, dl_speed, img_gb,
                    estimate.get("is_slow_machine", False),
                    estimate.get("warning_message"),
                )
        except Exception:
            pass

    if not yes:
        confirm_msg = (
            f"Acquire pod on {executor.huid} "
            f"({executor.gpu_count}×{executor.gpu_type}) "
            f"at ${executor.price_per_hour:.2f}/h?"
        )
        if not ui.confirm(confirm_msg):
            return

    if volume_create_params:
        action = CreateVolumeAction()
        result = ui.load(
            f"Creating volume '{volume_create_params['name']}'",
            lambda: action.execute({
                "lium": lium,
                "volume_create_params": volume_create_params
            })
        )

        if not result.ok:
            ui.error(result.error)
            return

        volume_id = result.data["volume_id"]

    action = RentPodAction()
    result = ui.load(
        "Renting machine",
        lambda: action.execute({
            "lium": lium,
            "executor": executor,
            "template": template,
            "dockerfile_content": dockerfile_content,
            "name": name,
            "volume_id": volume_id,
            "ports": ports,
            "ssh_name": ssh_name,
        })
    )

    if not result.ok:
        ui.error(result.error)
        return

    pod_id = result.data["pod_id"]
    pod_name = result.data["pod_name"]

    action = WaitReadyAction()
    result = ui.load(
        "Loading image",
        lambda: action.execute({
            "lium": lium,
            "pod_id": pod_id
        })
    )

    if not result.ok:
        ui.error(result.error)
        return

    pod = result.data["pod"]

    if termination_time:
        action = ScheduleTerminationAction()
        result = ui.load(
            "Scheduling termination",
            lambda: action.execute({
                "lium": lium,
                "pod": pod,
                "termination_time": termination_time
            })
        )

        if not result.ok:
            ui.error(result.error)

    if jupyter:
        action = InstallJupyterAction()
        result = ui.load(
            "Installing Jupyter",
            lambda: action.execute({
                "lium": lium,
                "pod": pod,
                "ui": ui
            })
        )

        if not result.ok:
            ui.error(result.error)

    # Docker-run mode: stream logs instead of SSH
    if docker_run_mode:
        from lium.cli.logs.actions import StreamLogsAction

        ui.dim(f"Streaming logs from {pod_name}... (Ctrl+C to stop)")

        ctx = {"lium": lium, "pod": pod, "tail": 100, "follow": True}
        action = StreamLogsAction()

        try:
            for line in action.execute(ctx):
                click.echo(line)
        except KeyboardInterrupt:
            ui.dim("\nStopped following logs")
        return

    # Standard mode: SSH into the pod
    action = PrepareSSHAction()
    result = ui.load(
        "Connecting SSH",
        lambda: action.execute({
            "pod_name": pod_name
        })
    )

    if not result.ok:
        ui.error(result.error)
        return

    ssh_cmd = result.data["ssh_cmd"]
    pod = result.data["pod"]

    from lium.cli.ssh.command import ssh_to_pod
    ssh_to_pod(ssh_cmd, pod)
