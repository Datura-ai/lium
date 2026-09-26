import json
import time
from datetime import datetime, timezone
from typing import Optional, Tuple
import click
import requests
from rich.markup import escape

from lium.sdk import (
    Lium,
    LiumAuthError,
    LiumError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
    PodStartError,
)
from lium.sdk.secrets import SECRETS_DISABLED, secrets_enabled, validate_secret_names
from lium.cli import ui
from lium.cli.secrets.command import redacted_usage_error
from lium.cli.workspaces.context import show_workspace
from lium.cli.utils import (
    CliFailure,
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_SSH_ERROR,
    _api_error_data,
    ensure_config,
    handle_errors,
    narrate_on_stderr_under_json,
    sdk_error_failure,
)
from lium.cli.ps.display import compact_pod
from lium.cli.completion import get_gpu_completions
from . import validation, parsing
from .budget import MIN_BUDGET_MINUTES, budget_deadline, budget_hours, rental_price_per_hour
from .actions import (
    ResolveExecutorAction,
    ResolveTemplateAction,
    CreateEphemeralTemplateAction,
    CreateVolumeAction,
    RentPodAction,
    WaitReadyAction,
    ScheduleTerminationAction,
    VerifyGpuCountAction,
    InstallJupyterAction,
    PrepareSSHAction,
    rented_gpu_count,
)

# The whole command's budget. Rentals start in ~25 s at the median and a cold image pull takes a
# few minutes; a wait that has passed fifteen minutes is a rent that is not going to come up on its
# own, and the caller is billed for every one of those minutes.
DEFAULT_TIMEOUT_SECONDS = 900


def _wait_budget(deadline: float, ready_timeout: Optional[int]) -> int:
    """Seconds the ready wait may take: what --timeout has left, capped by --ready-timeout."""
    remaining = max(1, int(deadline - time.monotonic()))
    return min(remaining, ready_timeout) if ready_timeout else remaining


def _schedule_termination_at_rent(lium: Lium, pod_id: str, pod_name: str, termination_time: datetime) -> bool:
    """Set --ttl/--until on the pod the rent just returned. True when the backend took it.

    A failure is reported, not raised: the pod exists and bills whatever happens here, so the
    command goes on to wait for it and schedules again once the pod is ready.
    """
    action = ScheduleTerminationAction()
    try:
        ui.load(
            "Scheduling termination",
            lambda: action.execute({"lium": lium, "pod": pod_id, "termination_time": termination_time}),
        )
    except (LiumError, requests.exceptions.RequestException) as exc:
        ui.warning(
            f"Auto-termination for pod {escape(pod_name)} (id: {escape(str(pod_id))}) was NOT scheduled ({escape(str(exc))}); "
            "it is tried again once the pod is ready"
        )
        return False
    ui.dim(
        f"removal of pod {escape(pod_name)} scheduled for {termination_time:%Y-%m-%d %H:%M UTC}"
        f"{_in_hours(termination_time)}, whether or not it becomes ready"
    )
    return True


def _termination_note(termination_time: Optional[datetime], scheduled: bool, pod_label: str) -> str:
    """The sentence a failed wait adds about --ttl/--until: set, failed, or nothing asked."""
    if not termination_time:
        return ""
    if scheduled:
        return (
            f" Auto-termination is scheduled for {termination_time:%Y-%m-%d %H:%M UTC}"
            f"{_in_hours(termination_time)}: the pod is removed then even if it never becomes ready."
        )
    return (
        " Auto-termination (--ttl/--until) was NOT scheduled (the schedule call failed after the rent); "
        f"set it with 'lium rm {pod_label} --in <duration>' or remove the pod."
    )


def _in_hours(termination_time: datetime) -> str:
    """`` (in 2.0h)`` for a time ahead of now; empty once it has passed."""
    hours = (termination_time - datetime.now(timezone.utc)).total_seconds() / 3600
    return f" (in {hours:.1f}h)" if hours > 0 else ""


def _post_rent_failure(exc: Exception, billing_pod: dict, doing: str, note: str = "") -> CliFailure:
    """The failure for an API call that broke after the rent: the pod exists and bills, so the
    envelope names it in ``data`` whatever the error was.

    A ``LiumError`` keeps its own code (``server_error``, ``rate_limited``, …). A
    ``requests.RequestException`` is the API not answering: ``Lium.ps()`` and ``schedule_termination``
    re-raise it once the SDK's retries run out, ``install_jupyter`` (a POST, sent once) on the first
    lost connection, and it used to fall through to ``unexpected_error`` with no ``data``. It gets
    ``api_timeout``, the code the rent itself uses when the API does not answer, and a message that
    names the pod. ``doing`` is the step that was running, in the pod's voice ("waiting for it to
    become ready"); ``note`` is what that step left undone and how to do it by hand.
    """
    if isinstance(exc, LiumError):
        return sdk_error_failure(exc, data=billing_pod, note=note)
    pod_name, pod_id = billing_pod["pod_name"], billing_pod["pod_id"]
    return CliFailure(
        "api_timeout",
        f"Pod {pod_name} (id: {pod_id}) is rented and billing, but the API stopped answering while "
        f"{doing} ({exc.__class__.__name__}).{note}",
        EXIT_API_ERROR,
        data=billing_pod,
        # The generic exit-3 hint says "Retry"; a second `lium up` would rent a second pod.
        hint=f"Do not run 'lium up' again: the pod exists. Check it with 'lium ps'; 'lium rm {pod_name}' removes it.",
    )


class UpCommand(click.Command):
    def parse_args(self, ctx, args):
        with_secret = any(arg == "--secret" or arg.startswith("--secret=") for arg in args)
        try:
            return super().parse_args(ctx, args)
        except click.UsageError as error:
            # with --secret on the line, a token click cannot place may be the secret's value
            if with_secret:
                raise redacted_usage_error(error, ctx) from None
            raise


@click.command("up", cls=UpCommand)
@click.argument("executor_id", required=False, metavar="NODE_ID")
@click.option("--name", "-n", help="Custom pod name")
@click.option("--template_id", "-t", help="Template ID")
@click.option("--volume", "-v", help="Volume spec: 'id:<HUID>' or 'new:name=<NAME>[,desc=<DESC>]'")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--gpu", help="Filter nodes by GPU type (e.g., H200, A6000)", shell_complete=get_gpu_completions)
@click.option("--count", "-c", type=int, help="Number of GPUs per pod (with NODE_ID: rent that many of the node's GPUs, if the provider allows splitting)")
@click.option("--country", help="Filter nodes by ISO country code (e.g., US, FR)")
@click.option("--min-cpus", "min_cpus", type=int, help="Minimum CPU thread count (the CPUs column of 'lium ls')")
@click.option("--ports", "-p", type=int, help="Minimum number of available ports required")
@click.option(
    "--ttl",
    help="Auto-terminate this long after the rent (e.g., 6h, 45m, 2d). Scheduled as soon as the pod exists, "
         "so it holds even if the pod never becomes ready.",
)
@click.option(
    "--until",
    help="Auto-terminate at time in local timezone (e.g., 'today 23:00', 'tomorrow 01:00', '2025-10-20 15:30'). "
         "Scheduled as soon as the pod exists, so it holds even if the pod never becomes ready.",
)
@click.option(
    "--budget",
    "budget_usd",
    type=click.FloatRange(min=0, min_open=True),
    metavar="USD",
    help="Auto-terminate once the pod has spent this much (price/h × uptime), scheduled client-side through the same removal mechanism as --ttl. Set once the pod is ready: a pod that never becomes ready is not capped by --budget (add --ttl for that). Combined with --ttl/--until the earlier deadline wins.",
)
@click.option("--jupyter", is_flag=True, help="Install Jupyter Notebook (automatically selects available port)")
@click.option("--no-ssh", "no_ssh", is_flag=True, help="Create the pod and return instead of opening an SSH session")
@click.option(
    "--timeout",
    "timeout",
    type=click.IntRange(min=1),
    default=DEFAULT_TIMEOUT_SECONDS,
    show_default=True,
    metavar="SECONDS",
    help="Time budget for the whole command: finding the node, renting it and waiting for the pod. If it runs out before the rent, exit 1 with no pod created; if it runs out while the pod is still starting, exit 1 with the pod named (it keeps running and billing).",
)
@click.option(
    "--ready-timeout",
    "ready_timeout",
    type=click.IntRange(min=1),
    default=None,
    metavar="SECONDS",
    help="Bound only the wait for the pod to become ready (exit 1, pod left running and named). Default: whatever --timeout leaves.",
)
@click.option(
    "--verify-gpus", "verify_gpus", is_flag=True,
    help="After the pod is ready, count the GPUs nvidia-smi sees over SSH and compare with the billed count",
)
@click.option(
    "--strict-gpus", "strict_gpus", is_flag=True,
    help="Remove the pod automatically when its GPU count does not match what was requested or billed "
         "(a pod that could not be checked over SSH is kept)",
)
@click.option("--restore-backup", "restore_backup_id", help="Backup ID to restore after the pod starts")
@click.option("--restore-to", "restore_path", help="New or empty subdirectory for the startup restore")
@click.option("--image", help="Docker image to run (e.g., pytorch/pytorch:2.0, nvidia/cuda:12.0)")
@click.option("--internal-ports", help="Internal ports to expose (comma-separated, e.g., 22,8000,8080)")
@click.option("--dockerfile", type=click.Path(exists=True, dir_okay=False, readable=True), help="Path to a Dockerfile to build the pod image from (custom build; mutually exclusive with --image/--template_id)")
@click.option("-e", "--env", multiple=True, help="Environment variables (KEY=VALUE), can be repeated")
@click.option(
    "--secret", "secret_names", multiple=True, metavar="NAME", hidden=not secrets_enabled(),
    help="Deliver a saved secret as /run/lium/secrets/NAME in the pod (not an env var); can be repeated. Experimental",
)
@click.option("--entrypoint", default="", help="Container entrypoint")
@click.option("--cmd", default="", help="Command to run in the container")
@click.option("--ssh-name", default=None, help="Name to register a new SSH key under (default: cli-<user>@<hostname>)")
@click.option(
    "--volume-encryption/--no-volume-encryption",
    default=True,
    help="Encrypt the local volume when supported (enabled by default)",
)
@click.option(
    "--json", "json_output", is_flag=True,
    help="Print the ready pod as machine-readable JSON on stdout (implies --no-ssh); progress goes to stderr, "
         "a failure is the JSON error envelope on stderr",
)
@handle_errors
@narrate_on_stderr_under_json
def up_command(
    executor_id: Optional[str],
    name: Optional[str],
    template_id: Optional[str],
    volume: Optional[str],
    yes: bool,
    gpu: Optional[str],
    count: Optional[int],
    country: Optional[str],
    min_cpus: Optional[int],
    ports: Optional[int],
    ttl: Optional[str],
    until: Optional[str],
    budget_usd: Optional[float],
    jupyter: bool,
    no_ssh: bool,
    timeout: int,
    ready_timeout: Optional[int],
    verify_gpus: bool,
    strict_gpus: bool,
    restore_backup_id: Optional[str],
    restore_path: Optional[str],
    image: Optional[str],
    internal_ports: Optional[str],
    dockerfile: Optional[str],
    env: Tuple[str, ...],
    secret_names: Tuple[str, ...],
    entrypoint: Optional[str],
    cmd: Optional[str],
    ssh_name: Optional[str],
    volume_encryption: bool,
    json_output: bool,
):
    """\b
    Create a new GPU pod on a node.
    \b
    The node's $/h price is charged per second, from deploy until the pod is
    removed, with no minimum.
    \b
    NODE_ID: Node UUID, HUID, or index from last 'lium ls'.
    If not provided, the filters pick the node and the pick is printed before renting.
    With --gpu the backend chooses: the cheapest $/GPU·h node matching the filters
    (one GPU unless -c) with no download floor, so a node 'lium ls --gpu X' shows is
    not skipped, rented in the same call; a pick taken meanwhile falls through to the
    next node at or below the confirmed price. Without
    --gpu (or on an older backend) row 1 of 'lium ls' with the same filters is rented:
    one rule for both commands, cheapest $/GPU·h first, nodes without a price last.
    \b
    Examples:
      lium up cosmic-hawk-f2                # Create pod on specific node
      lium up 1                             # Create pod on node #1 from last ls
      lium up --gpu H200                    # Auto-select cheapest H200 node
      lium up --gpu A6000 -c 2              # Auto-select cheapest 2×A6000 node
      lium up cosmic-hawk-f2 -c 1           # Rent 1 GPU of a splittable multi-GPU node
      lium up --country US                  # Row 1 of 'lium ls --country US'
      lium up --gpu H200 --country FR       # Combine multiple filters
      lium up --gpu H100 --min-cpus 32      # Only nodes with at least 32 CPU threads
      lium up --ports 5                     # Auto-select with minimum 5 ports
      lium up 1 --name my-pod               # Create with custom name
      lium up 1 --volume id:brave-fox-3a    # Attach existing volume by HUID
      lium up 1 --volume new:name=my-data   # Create and attach new volume
      lium up 1 --volume new:name=my-data,desc="Training data"  # With description
      lium up 1 --ttl 6h                    # Auto-terminate after 6 hours
      lium up --gpu H100 --timeout 600      # Give the whole rent 10 minutes, then exit 1 naming the pod
      lium up 1 --until "today 23:00"       # Auto-terminate at 23:00 local time today
      lium up 1 --until "tomorrow 01:00"    # Auto-terminate at 01:00 local time tomorrow
      lium up --gpu H100 --budget 12.50     # Auto-terminate once $12.50 has been spent
      lium up 1 --jupyter                   # Install Jupyter Notebook (auto-selects port)
      lium up --gpu H200 -c 8 --verify-gpus # Fail if the pod exposes fewer GPUs than billed
      lium up --gpu H200 -c 8 --verify-gpus --strict-gpus  # ...and remove the pod on mismatch
      lium up 1 --restore-backup BACKUP_ID --restore-to /root/restored
      lium up --gpu H100 -y --json          # Rent, wait, print the pod as JSON (no SSH session)
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
    deadline = time.monotonic() + timeout

    # Check if we're in docker-run mode or custom-Dockerfile build mode
    docker_run_mode = image is not None
    dockerfile_mode = dockerfile is not None

    valid, error = validation.validate(
        executor_id, gpu, count, country, ttl, until, image, template_id, dockerfile, min_cpus
    )
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)
    if bool(restore_backup_id) != bool(restore_path):
        raise CliFailure(
            "invalid_arguments",
            "--restore-backup and --restore-to must be provided together",
            EXIT_CONFIGURATION_ERROR,
        )

    # Parse env vars if provided
    env_dict = {}
    if env:
        env_dict, error = validation.parse_env_vars(env)
        if error:
            raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    if secret_names:
        if not secrets_enabled():
            raise CliFailure("secrets_disabled", SECRETS_DISABLED, EXIT_CONFIGURATION_ERROR)
        try:
            secret_names = tuple(validate_secret_names(secret_names))
        except ValueError as e:
            raise CliFailure("invalid_arguments", str(e), EXIT_CONFIGURATION_ERROR)

    parsed, error = parsing.parse(ttl, until, volume)
    if error:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    termination_time = parsed.get("termination_time")  # --until; --ttl becomes a time at the rent
    ttl_duration = parsed.get("ttl")
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
            raise CliFailure(
                "invalid_arguments",
                f"{', '.join(unsupported)} cannot be combined with --dockerfile "
                "(the Dockerfile defines the image's env, entrypoint, command, and ports)",
                EXIT_CONFIGURATION_ERROR,
            )

        try:
            dockerfile_content = Path(dockerfile).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CliFailure(
                "unreadable_dockerfile",
                f"Could not read Dockerfile: {exc}",
                EXIT_CONFIGURATION_ERROR,
            )
        if not dockerfile_content.strip():
            raise CliFailure(
                "empty_dockerfile", "Dockerfile is empty", EXIT_CONFIGURATION_ERROR
            )
        max_bytes = 64 * 1024
        size_bytes = len(dockerfile_content.encode("utf-8"))
        if size_bytes > max_bytes:
            raise CliFailure(
                "dockerfile_too_large",
                f"Dockerfile is too large ({size_bytes} bytes); max is {max_bytes} bytes (64 KiB)",
                EXIT_CONFIGURATION_ERROR,
            )

    lium = Lium(source="cli")
    # the billing owner of this workspace pays for the pod (lium-platform DAH-2986)
    show_workspace(lium, acting=True)
    if restore_backup_id:
        restore_backup_id = ui.load(
            "Resolving backup ID", lambda: lium.resolve_backup_id(restore_backup_id)
        )

    action = ResolveExecutorAction()
    result = ui.load(
        "Finding node",
        lambda: action.execute({
            "lium": lium,
            "executor_id": executor_id,
            "gpu": gpu,
            "count": count,
            "country": country,
            "min_cpus": min_cpus,
            "ports": ports,
            "template_id": template_id,
            "dockerfile_content": dockerfile_content,
        })
    )

    if not result.ok:
        # the spec path's 409 carries the server's hint and request_id in data (DAH-3057): the
        # hint becomes the failure's own (the envelope's error.hint), the id stays in data
        data = dict(result.data or {})
        hint = data.pop("hint", None)
        error = result.error
        if secret_names and executor_id and executor_id in (error or ""):
            # `lium up --secret NAME VALUE` puts the value where NODE_ID goes
            error = (
                "No node matches NODE_ID (not shown: with --secret it may be a secret value); "
                "run 'lium ls --format json' for the ids rentable now"
            )
        raise CliFailure("node_selection_failed", error, EXIT_GENERAL_ERROR, data=data or None, hint=hint)

    executor = result.data["executor"]
    # What the rental bills: the server's figure when it picked (a split of a larger node
    # costs price_per_gpu × count, not the node's total), else the node's total $/h.
    server_price_per_hour = result.data.get("price_per_hour")
    price_per_hour = server_price_per_hour or executor.price_per_hour
    # The GPUs the rental gets, next to what they cost: on the spec path the server may rent a
    # split of a larger node, so the node's own count would overstate it.
    gpu_count = result.data.get("gpu_count") or executor.gpu_count
    spec = result.data.get("spec")
    if result.data.get("auto_selected"):
        # Name the pick and its total $/h before anything is billed: with -y the
        # confirmation below is skipped and the price would first appear in `ps`.
        country = (executor.location or {}).get("country") or (executor.location or {}).get("country_code")
        ui.info(
            f"Selected {ui.styled(executor.huid, 'id')} "
            f"({gpu_count}×{executor.gpu_type}{', ' + country if country else ''}) "
            f"at ${price_per_hour:.2f}/h — "
            + (
                f"cheapest of {result.data['candidates']} matching node(s)"
                if spec
                else f"row 1 of 'lium ls' with these filters ({result.data['candidates']} listed)"
            )
        )

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
                raise CliFailure(
                    "invalid_ports",
                    "Invalid port format. Use comma-separated integers (e.g., 22,8000,8080)",
                    EXIT_CONFIGURATION_ERROR,
                )

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
        template = result.data["template"]
    else:
        action = ResolveTemplateAction()
        result = action.execute({
            "lium": lium,
            # the server's dry run already named the node's recommended template
            "template_id": template_id or result.data.get("template_id"),
            "executor": executor
        })
        if not result.ok:
            raise CliFailure("template_failed", result.error, EXIT_GENERAL_ERROR)
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

    if budget_usd is not None:
        # What the rental will bill. On the spec path the server priced the pick (price_per_hour
        # above, #209) and that figure stands. On the node path: price_per_gpu × GPUs for a split,
        # the node's total otherwise; no --count rents the node's free GPUs (rented_gpu_count), not
        # the whole host — on a partially rented split host the host total would overstate the
        # price and refuse a valid budget.
        if server_price_per_hour is None:
            price_per_hour = rental_price_per_hour(
                executor, count if count is not None else rented_gpu_count(executor)
            )
        hours = budget_hours(budget_usd, price_per_hour)
        if hours is None:
            raise CliFailure(
                "invalid_arguments",
                f"Cannot apply --budget: node {executor.huid} has no hourly price",
                EXIT_CONFIGURATION_ERROR,
            )
        if hours * 60 < MIN_BUDGET_MINUTES:
            raise CliFailure(
                "invalid_arguments",
                f"--budget {budget_usd:.2f} buys {hours * 60:.1f} min at ${price_per_hour:.2f}/h; "
                f"the minimum is {MIN_BUDGET_MINUTES} min (${price_per_hour * MIN_BUDGET_MINUTES / 60:.2f})",
                EXIT_CONFIGURATION_ERROR,
            )
        ui.dim(f"Budget ${budget_usd:.2f} at ${price_per_hour:.2f}/h ≈ {hours:.1f}h of runtime")
    # `lium up <node> -c N` rents N of the node's GPUs. The count is always sent, the host total
    # included: the API refuses a count the node cannot serve right now before anything is billed,
    # and the provider's splitting policy decides whether N is allowed. Without -c the rent takes
    # the node's free GPUs, so the prompt names that count and its price. On the spec path the
    # server already picked the split and priced it (gpu_count / price_per_hour above).
    requested_gpu_count = count if executor_id and count else None
    if spec:
        rent_count, rent_price = gpu_count, price_per_hour
    else:
        rent_count = requested_gpu_count or rented_gpu_count(executor)
        # What the API would refuse anyway is refused here, before a prompt that would read
        # "0×H200 of 4 at $0.00/h" or "5×H200 of 2". A count within the host but above the
        # free GPUs is left to the API: the provider's splitting policy decides that one.
        if rent_count < 1:
            raise CliFailure(
                "no_free_gpus",
                f"No GPU of {executor.huid} is free right now ({executor.gpu_count} on the node, all rented).",
                EXIT_GENERAL_ERROR,
            )
        if rent_count > executor.gpu_count:
            raise CliFailure(
                "invalid_arguments",
                f"-c {rent_count}: {executor.huid} has {executor.gpu_count} GPU(s).",
                EXIT_CONFIGURATION_ERROR,
            )
        rent_price = executor.price_per_gpu * rent_count if rent_count != executor.gpu_count else price_per_hour

    if not yes:
        confirm_msg = (
            f"Acquire pod on {executor.huid} "
            f"({rent_count}×{executor.gpu_type}"
            f"{f' of {executor.gpu_count}' if rent_count != executor.gpu_count else ''}) "
            f"at ${rent_price:.2f}/h?"
        )
        if restore_backup_id:
            confirm_msg += f" Restore backup {restore_backup_id} to {restore_path} after startup."
        asked_at = time.monotonic()
        if not ui.confirm(confirm_msg):
            return
        # The prompt waits on a person, not on Lium: the time spent answering it is not part of
        # the --timeout budget, or a slow answer would kill the command before it rents.
        deadline += time.monotonic() - asked_at

    # A volume `--volume new:` creates here exists before the rent and is kept by every failure
    # below (nothing removes it). Those failures name it in the message and carry it in the
    # envelope's ``data``: ``volume_id`` (the API id, what the SDK takes) and ``volume_huid``
    # (what `--volume id:<HUID>` and `lium volumes` show), so a program can rent again with the
    # same volume or remove it without parsing the message. Empty when no volume was created:
    # the envelope then has no ``data``.
    created_volume: dict = {}
    kept = ""
    if volume_create_params:
        action = CreateVolumeAction()
        result = ui.load(
            f"Creating volume '{volume_create_params['name']}'",
            lambda: action.execute({
                "lium": lium,
                "volume_create_params": volume_create_params
            })
        )

        volume_id = result.data["volume_id"]
        created_volume = {"volume_id": volume_id, "volume_huid": result.data["volume"].huid}
        kept = f" The volume {volume_create_params['name']} was created and is kept."

    # --timeout is the whole command's budget. Everything before this line (finding the node and
    # the template, creating the volume — not the answer at the prompt) counts against it; a budget
    # that is already spent must not reach the rent, or the pod would be created and then
    # reported as timed out one second later. The rent is the first thing that bills.
    if time.monotonic() >= deadline:
        raise CliFailure(
            "timeout_before_rent",
            f"The --timeout budget of {timeout}s ran out before renting {executor.huid}; no pod was created.{kept} "
            "Run again with a larger --timeout.",
            EXIT_GENERAL_ERROR,
            data=created_volume,
        )

    ui.dim(f"renting {executor.huid}…")
    action = RentPodAction()
    try:
        result = ui.load(
            "Renting machine",
            lambda: action.execute({
                "lium": lium,
                "executor": executor,
                "spec": spec,
                "template": template,
                "dockerfile_content": dockerfile_content,
                "name": name,
                "volume_id": volume_id,
                "ports": ports,
                "ssh_name": ssh_name,
                "enable_volume_encryption": volume_encryption,
                "backup_id": restore_backup_id,
                "restore_path": restore_path,
                "gpu_count": requested_gpu_count,
                "secret_names": list(secret_names),
            })
        )
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        # The API did not answer the rent request. Whether a pod was created is unknown, so the
        # caller must look before renting again — a blind retry is how one `up` made two pods.
        raise CliFailure(
            "api_timeout",
            f"The rent request for {executor.huid} got no answer from the API ({exc.__class__.__name__}). "
            f"Run 'lium ps' before retrying: a pod named {name or executor.huid} may exist and be billing.{kept}",
            EXIT_API_ERROR,
            data=created_volume,
        )
    except (LiumAuthError, LiumPermissionError, LiumServerError, LiumRateLimitError) as exc:
        # handle_errors already names these (bad key, no permission, server down, throttled);
        # only a volume created above has to ride along, with the same code, exit and hint.
        if created_volume:
            raise sdk_error_failure(exc, data=created_volume, note=kept)
        raise
    except LiumError as exc:
        # The API answered and said no: the node is no longer rentable (taken, offline, pending
        # rental) or the request was refused. Usually no pod exists — but Lium.up() sends the
        # rent a second time when the first request got no answer, and a refusal of that retry
        # can mean the first one did create a pod. So point at 'lium ps' instead of promising
        # that nothing was created. (On the spec path Lium.rent posts once and already looked
        # the pod up by name before raising, so the hint is only conservative there.)
        # The server's code, hint and request_id ride along (DAH-3057): the code replaces the
        # generic rent_rejected, the hint replaces the default one, the id is printed under it.
        raise CliFailure(
            exc.code or "rent_rejected",
            f"Node {executor.huid} could not be rented: {exc}. Run 'lium ps' to check whether a pod was created. "
            f"Run 'lium ls --format json' for the nodes rentable now.{kept}",
            EXIT_API_ERROR,
            # the server's request_id (DAH-3057) next to the volume the rent leaves behind
            data={**(_api_error_data(exc) or {}), **created_volume} or None,
            hint=exc.hint,
        )

    pod_id = result.data["pod_id"]
    pod_name = result.data["pod_name"]
    rented = result.data.get("executor") or executor
    # The GPUs this rental got: on the spec path the server's figure (one GPU unless -c, possibly
    # a split of a larger node), else the node's count from the dry run. Kept here because
    # `result` is reused by the actions below, and the GPU-count check needs it.
    rented_gpus = result.data.get("gpu_count") or gpu_count
    if rented.id != executor.id:
        # The confirmed node was taken between the dry run and the rent; the server took the
        # next candidate at or below the confirmed $/GPU·h.
        ui.info(
            f"{ui.styled(executor.huid, 'id')} was taken meanwhile; rented "
            f"{ui.styled(rented.huid, 'id')} ({rented_gpus or rented.gpu_count}×{rented.gpu_type}) "
            f"at ${result.data['price_per_hour']:.2f}/h instead"
        )
    executor = rented
    ui.dim(f"pod {pod_name} (id: {pod_id}) created; waiting for it to become ready")

    # The pod is rented and already billing from here on. Every failure below
    # names it before propagating, or the caller cannot clean up what it pays for.
    # In the JSON envelope the name lives in ``data`` too, so a program does not
    # have to parse it out of the message.
    billing_pod = {"pod_id": pod_id, "pod_name": pod_name}
    #
    # --ttl/--until are scheduled now, with the id the rent returned, not once the pod is ready:
    # a pod that never gets there (a wait that runs out, a stuck pull) bills all the same, and
    # the backend removes a scheduled pod in any status but DELETING. --ttl counts from here,
    # so the node lookup and the prompt above did not eat into it. A schedule call that fails
    # here does not end the command — the pod is kept and the schedule is tried once more when
    # the pod is ready — but the caller hears it right away.
    termination_scheduled = False
    if ttl_duration:
        termination_time = datetime.now(timezone.utc) + ttl_duration
    if termination_time:
        termination_scheduled = _schedule_termination_at_rent(lium, pod_id, pod_name, termination_time)

    wait_timeout = _wait_budget(deadline, ready_timeout)
    action = WaitReadyAction()
    try:
        result = ui.load(
            "Loading image",
            lambda: action.execute({
                "lium": lium,
                "pod_id": pod_id,
                "timeout": wait_timeout,
                "report": ui.dim,
            })
        )
    except PodStartError as exc:
        # The pod is dead (FAILED/CREATION_FAILED/STOPPED) or gone; say so with its last status
        # and the cause the backend recorded, so a script does not retry a rent that will never
        # come up — and can tell an unreachable host from a bad image.
        label = exc.pod.huid if exc.pod is not None else pod_name
        raise CliFailure(
            "pod_start_failed",
            f"Pod {label} (id: {pod_id}) failed to start: {exc}."
            f"{_termination_note(termination_time, termination_scheduled, label)} "
            f"Check 'lium ps' and remove it with 'lium rm {label}' if it is still listed.",
            EXIT_API_ERROR,
            data=billing_pod,
        )
    except Exception as exc:
        ui.error(f"Pod {pod_name} (id: {pod_id}) was created but did not become ready")
        if isinstance(exc, (LiumError, requests.RequestException)):
            # The API failed mid-wait (5xx, 429, a revoked key) or stopped answering (`Lium.ps()`
            # re-raises the transport error after its retries); the pod may well be up and bills.
            raise _post_rent_failure(exc, billing_pod, "waiting for it to become ready")
        raise

    if not result.ok:
        # Still starting when the budget ran out: the pod keeps billing, so
        # name it and hand the decision back to the caller — with the backend's
        # own estimate and phase when it sent them, so a slow pull reads
        # differently from a stuck pod — and with its removal time, when there is one.
        hint = result.data.get("eta_hint")
        backend = f" (backend: {hint})" if hint else ""
        # A --budget cap is computed from the ready pod (below), so a wait that gives up leaves the pod
        # uncapped by it — say so and name the remedy; --ttl/--until were scheduled at the rent and
        # _termination_note covers them.
        budget_not_scheduled = (
            f" The --budget cap was NOT scheduled (it is computed from the ready pod); cap it with "
            f"'lium rm {pod_name} --in <duration>' or remove the pod."
            if budget_usd is not None else ""
        )
        raise CliFailure(
            "pod_not_ready",
            f"Pod {pod_name} (id: {pod_id}) is still starting after {wait_timeout}s and is billing{backend}."
            f"{_termination_note(termination_time, termination_scheduled, pod_name)}{budget_not_scheduled} "
            f"Wait with 'lium ps', or remove it with 'lium rm {pod_name}'.",
            EXIT_GENERAL_ERROR,
            data=billing_pod,
        )

    pod = result.data["pod"]
    pod_label = f"Pod {ui.styled(pod.huid, 'pod_id')} (name: {escape(pod_name)}, id: {escape(str(pod_id))})"

    if budget_usd is not None:
        # The cap counts from the pod's created_at, the one timestamp the API gives (the platform
        # bills from RUNNING but does not say when that began; lium spend and lium rm count from
        # the same point), at the pod's own price. With --ttl/--until too, the earlier wins.
        deadline = budget_deadline(pod, budget_usd, fallback_price=price_per_hour)
        if deadline is None:
            ui.warning(f"{pod_label} is running but the --budget cap could not be computed (no price or created_at)")
        elif deadline <= datetime.now(timezone.utc):
            # Starting took longer than the whole budget buys. A past deadline is a 400 from
            # the API and the pod would run uncapped, so the cap is applied the only way
            # left: the pod goes now, and the caller hears why.
            ui.warning(f"{pod_label} took longer to start than its ${budget_usd:.2f} budget buys; removing it")
            lium.down(pod)
            raise CliFailure(
                "budget_exhausted",
                f"Pod {pod.huid} became usable only after the {hours * 60:.0f} min that ${budget_usd:.2f} buys "
                f"at ${price_per_hour:.2f}/h (created {pod.created_at}); it has been removed. Pick a larger "
                "--budget or a cheaper node ('lium ls --sort price_total')",
                EXIT_GENERAL_ERROR,
            )
        elif termination_time is None or deadline < termination_time:
            termination_time = deadline
            termination_scheduled = False  # the cap is earlier than what the rent scheduled; schedule it below
            ui.dim(f"Spend cap ${budget_usd:.2f}: removal scheduled for {deadline:%Y-%m-%d %H:%M UTC}")

    if termination_time and not termination_scheduled:
        # The schedule call failed right after the rent, or --budget set an earlier deadline than
        # the rent scheduled; the pod is ready now, so schedule it before anything else runs on the
        # pod. A failure ends the command: the caller asked for an end time and must not read a
        # ready pod as having one.
        action = ScheduleTerminationAction()
        try:
            ui.load(
                "Scheduling termination",
                lambda: action.execute({
                    "lium": lium,
                    "pod": pod,
                    "termination_time": termination_time
                })
            )
        except Exception as exc:
            ui.info(f"{pod_label} is running but auto-termination was NOT scheduled")
            if isinstance(exc, (LiumError, requests.RequestException)):
                raise _post_rent_failure(
                    exc, billing_pod, "scheduling its auto-termination",
                    note=f" Auto-termination is NOT scheduled; set it with 'lium rm {pod.huid} --in <duration>' or remove the pod.",
                )
            raise
        ui.dim(f"removal of pod {escape(pod_name)} scheduled for {termination_time:%Y-%m-%d %H:%M UTC}{_in_hours(termination_time)}")

    # The GPU count is checked after --ttl is scheduled: a mismatched pod that is
    # left running (no --strict-gpus) must still terminate when the caller asked.
    # The requested count is --count, or, when there was none, what the rent got: on the spec
    # path the server's figure (one GPU unless -c — a correct 1-GPU rent of an 8-GPU node must
    # not read as a mismatch), else the node's free GPUs, since a rent without a count takes
    # all of them, not the host total.
    action = VerifyGpuCountAction()
    verify_ctx = {
        "lium": lium,
        "pod": pod,
        "expected_count": count if count is not None else (rented_gpus if spec else rented_gpu_count(executor)),
        "executor_id": executor.id,
        "verify_via_ssh": verify_gpus,
    }
    if verify_gpus:
        result = ui.load("Verifying GPU count", lambda: action.execute(verify_ctx))
    else:
        result = action.execute(verify_ctx)
    if not result.ok:
        ui.error(result.error)
        if strict_gpus and result.data.get("mismatch"):
            try:
                ui.load("Removing pod", lambda: lium.rm(pod))
            except (LiumError, requests.RequestException) as exc:
                # The pod is still there and bills (or, after a lost connection on the DELETE, may
                # be): say so under the same code, with pod_removed false, and let 'lium ps' settle it.
                # the server's request_id and hint ride along when the DELETE was refused (DAH-3057)
                api = exc if isinstance(exc, LiumError) else None
                raise CliFailure(
                    "gpu_count_mismatch",
                    f"{result.error}; removing the pod failed: {exc}",
                    EXIT_GENERAL_ERROR,
                    data={**result.data, **billing_pod, "pod_removed": False, **((api and _api_error_data(api)) or {})},
                    hint=api.hint if api else None,
                )
            ui.info(f"{pod_label} removed (--strict-gpus)")
            raise CliFailure(
                "gpu_count_mismatch",
                f"{result.error}; pod removed",
                EXIT_GENERAL_ERROR,
                data={**result.data, **billing_pod, "pod_removed": True},
            )
        ui.info(f"{pod_label} is running with the GPU count above; remove it with 'lium rm {pod.huid}'")
        raise CliFailure(
            "gpu_count_mismatch" if result.data.get("mismatch") else "gpu_verification_failed",
            result.error,
            EXIT_GENERAL_ERROR,
            data={**result.data, **billing_pod},
        )

    if jupyter:
        action = InstallJupyterAction()
        try:
            result = ui.load(
                "Installing Jupyter",
                lambda: action.execute({
                    "lium": lium,
                    "pod": pod,
                    "ui": ui
                })
            )
        except Exception as exc:
            ui.info(f"{pod_label} is running but Jupyter was NOT installed")
            if isinstance(exc, (LiumError, requests.RequestException)):
                raise _post_rent_failure(
                    exc, billing_pod, "installing Jupyter",
                    note=f" Jupyter is NOT installed; add it with 'lium update {pod.huid} --jupyter <port>'.",
                )
            raise

        if not result.ok:
            ui.info(pod_label)
            raise CliFailure(
                "jupyter_install_failed",
                f"Pod is running but Jupyter was NOT installed: {result.error}",
                EXIT_GENERAL_ERROR,
                data=billing_pod,
                # The pod exists and bills; running `up` again would rent a second one.
                hint=f"The pod is up: add Jupyter with 'lium update {pod.huid} --jupyter <port>' "
                     "instead of running 'lium up' again",
            )
        # ``pod`` was fetched before the install; the JSON below must show the URL it produced.
        pod.jupyter_url = result.data.get("jupyter_url") or pod.jupyter_url

    # Always state what was created: a caller that only gets an SSH banner or a
    # log stream has no way to name the pod it is now paying for.
    ui.info(f"{pod_label} ready")

    if restore_backup_id:
        ui.warning(
            f"Restore is continuing in {restore_path}. Do not modify that directory until the restore completes."
        )

    if json_output:
        # The same pod view as `lium ps --format json`, so one parser serves both, printed the
        # way `ps`, `describe` and `rm --format json` print theirs: the bare payload, indented,
        # no `ok` wrapper (success is exit 0; a failure is the envelope on stderr). The SSH
        # session and the log stream are for a person, so --json ends here.
        payload = {"pod": compact_pod(pod)}
        if termination_time:
            # `--ttl`, `--until` or the `--budget` cap: the time the backend was given. Present only
            # when one was set, as `rm --format json` carries `termination_time` only for a schedule.
            payload["termination_time"] = termination_time.isoformat()
        click.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    if no_ssh:
        return

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

    ssh_argv = result.data["ssh_argv"]
    pod = result.data["pod"]

    from lium.cli.ssh.command import ssh_session_connected

    if not ssh_session_connected(ssh_argv):
        raise CliFailure(
            "ssh_connection_failed",
            f"Pod {pod.huid} is running but the SSH connection failed",
            EXIT_SSH_ERROR,
        )
