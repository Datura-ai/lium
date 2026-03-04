"""GPU splitting setup commands."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.box import SIMPLE_HEAVY
from rich.panel import Panel
from rich.prompt import Confirm
from rich.table import Table

from ..utils import console, timed_step_status
from .docker_config import build_verification_result, inspect_docker_state, load_daemon_json, merge_overlay2
from .models import CandidateEvaluation, CommandResult, PreflightResult, SetupPlan, VerificationResult
from .storage import auto_select_target, evaluate_candidates, load_block_devices, load_mount_info, resolve_explicit_target


def run_command(args: list[str], check: bool = True, capture: bool = True) -> CommandResult:
    """Run one host command and normalize its output.

    This is the only low-level subprocess wrapper used by the GPU-splitting
    flow. Callers pass the exact argv list that should be executed, for
    example:

    - `["systemctl", "stop", "docker"]`
    - `["lsblk", "--json", ...]`
    - `["parted", "-s", "/dev/nvme1n1", "mklabel", "gpt"]`
    - `["mount", "/var/lib/docker"]`

    Keeping command execution centralized here makes it easier to:

    - show the full failing command in error messages
    - stub host interactions in tests
    - avoid `shell=True` for the new storage-migration code
    """
    result = subprocess.run(args, text=True, capture_output=capture)
    command_result = CommandResult(
        args=args,
        stdout=result.stdout or "",
        stderr=result.stderr or "",
        returncode=result.returncode,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"--- stdout ---\n{command_result.stdout[:4000]}\n"
            f"--- stderr ---\n{command_result.stderr[:4000]}"
        )
    return command_result


def gpu_command_errors(func):
    """Convert runtime failures into Click errors with non-zero exit codes."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except click.ClickException:
            raise
        except click.Abort:
            raise
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


def _require_binary(name: str):
    if shutil.which(name) is None:
        raise RuntimeError(f"Required tool not found: {name}")


def _detect_distro() -> Optional[str]:
    release = Path("/etc/os-release")
    if not release.exists():
        return None
    values = {}
    for line in release.read_text().splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"')
    return values.get("ID")


def _preflight() -> PreflightResult:
    """Collect the host facts required before any destructive planning.

    Commands run directly here:

    - `systemctl --version` to prove `systemd` tooling is available

    Commands delegated through helpers:

    - `findmnt --json /` via `load_mount_info(...)`
    - `docker info --format '{{json .}}'` via `inspect_docker_state(...)`
    - `findmnt --json <docker-root>` via `load_mount_info(...)`
    - `lsblk --json --bytes ...` via `load_block_devices(...)`

    This function must stay read-only. Its job is to confirm the OS/runtime
    boundary, discover the current Docker storage layout, and capture a block
    device inventory for later target resolution.
    """
    if os.geteuid() != 0:
        raise RuntimeError("gpu-splitting requires root privileges")
    if platform.system() != "Linux":
        raise RuntimeError("gpu-splitting is only supported on Linux")

    distro_id = _detect_distro()
    if distro_id not in {"ubuntu", "debian"}:
        raise RuntimeError("gpu-splitting is currently supported only on Ubuntu/Debian hosts")

    for binary in [
        "docker",
        "systemctl",
        "rsync",
        "lsblk",
        "findmnt",
        "parted",
        "blkid",
        "mount",
        "umount",
        "partprobe",
        "mkfs.xfs",
        "xfs_info",
    ]:
        _require_binary(binary)

    run_command(["systemctl", "--version"], True, True)
    root_mount = load_mount_info(run_command, "/")
    docker_state = inspect_docker_state(run_command)
    docker_mount = load_mount_info(run_command, docker_state.docker_root_dir)
    block_devices = load_block_devices(run_command)
    return PreflightResult(
        os_name=platform.system(),
        distro_id=distro_id,
        root_mount=root_mount,
        docker_mount=docker_mount,
        docker_state=docker_state,
        block_devices=block_devices,
    )


def _build_setup_plan(preflight: PreflightResult, device: Optional[str]) -> SetupPlan:
    """Build the full destructive plan without mutating the host.

    Commands run directly here:

    - `blkid -s UUID -o value <target>` when reusing an existing partition and
      we want to know whether `/etc/fstab` already matches

    Commands delegated through helpers:

    - explicit device validation / auto-selection from `storage.py`
    - `/etc/docker/daemon.json` load + merge from `docker_config.py`

    The result is the exact plan that `check` prints and `setup` confirms
    before the first host mutation.
    """
    if device:
        target = resolve_explicit_target(
            run_command,
            preflight.block_devices,
            preflight.root_mount,
            preflight.docker_mount,
            device,
        )
    else:
        target = auto_select_target(
            run_command,
            preflight.block_devices,
            preflight.root_mount,
            preflight.docker_mount,
        )

    daemon_json_path = "/etc/docker/daemon.json"
    existing_daemon = load_daemon_json(daemon_json_path)
    merged_daemon, daemon_changed = merge_overlay2(existing_daemon)
    target_path = target.created_partition_path or target.resolved_target
    uuid = ""
    if not target.needs_partition_creation:
        try:
            uuid = _get_uuid(target_path)
        except RuntimeError:
            uuid = ""

    destructive_actions = []
    if target.needs_new_gpt:
        destructive_actions.append(f"create GPT label on {target.parent_disk}")
    if target.needs_partition_creation:
        destructive_actions.append(f"create partition on {target.parent_disk}")
    if target.needs_format:
        destructive_actions.append(f"format target as XFS: {target.resolved_target}")
    destructive_actions.extend(
        [
            f"stop Docker services for {preflight.docker_state.docker_root_dir}",
            "back up Docker data",
            f"mount target at {preflight.docker_state.docker_root_dir} with pquota",
            f"update {daemon_json_path}",
            "update /etc/fstab",
            "restore Docker data",
            "restart Docker",
        ]
    )

    backup_dir = f"{preflight.docker_state.docker_root_dir}.lium-backup-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    return SetupPlan(
        preflight=preflight,
        target=target,
        backup_dir=backup_dir,
        daemon_json_path=daemon_json_path,
        daemon_json_changed=daemon_changed,
        daemon_json_merged=merged_daemon,
        fstab_path="/etc/fstab",
        fstab_changed=_fstab_would_change("/etc/fstab", uuid) if uuid else True,
        destructive_actions=destructive_actions,
    )


def _format_bool(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _plan_table(plan: SetupPlan) -> Table:
    table = Table(title="GPU Splitting Setup Plan", show_header=False, box=SIMPLE_HEAVY)
    table.add_column("Item", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row("Docker root", plan.preflight.docker_state.docker_root_dir)
    table.add_row("Selected target", plan.target.resolved_target)
    table.add_row("Selection mode", plan.target.selection_mode)
    table.add_row("Target classification", plan.target.classification)
    table.add_row("Create partition", "yes" if plan.target.needs_partition_creation else "no")
    table.add_row("Format target", "yes" if plan.target.needs_format else "no")
    table.add_row("Backup directory", plan.backup_dir)
    table.add_row("daemon.json update", "yes" if plan.daemon_json_changed else "no")
    table.add_row("fstab update", "yes" if plan.fstab_changed else "no")
    return table


def _docker_state_table(preflight: PreflightResult) -> Table:
    table = Table(title="Current Docker State", box=SIMPLE_HEAVY)
    table.add_column("Field", style="cyan")
    table.add_column("Value")
    state = preflight.docker_state
    table.add_row("Docker root", state.docker_root_dir)
    table.add_row("Storage driver", state.storage_driver or "unknown")
    table.add_row("Backing filesystem", state.backing_filesystem or "unknown")
    table.add_row("Supports d_type", _format_bool(state.supports_d_type))
    table.add_row("Docker info available", "yes" if state.docker_info_available else "no")
    table.add_row("Docker service running", "yes" if state.service_running else "no")
    return table


def _candidate_table(candidates: list[CandidateEvaluation]) -> Table:
    table = Table(title="Storage Candidates", box=SIMPLE_HEAVY)
    table.add_column("Path", style="cyan")
    table.add_column("Type")
    table.add_column("Allowed")
    table.add_column("Reason")
    for candidate in candidates:
        table.add_row(
            candidate.path,
            candidate.device_type,
            "yes" if candidate.allowed else "no",
            candidate.details,
        )
    return table


def _verification_table(result: VerificationResult, title: str = "Verification") -> Table:
    table = Table(title=title, box=SIMPLE_HEAVY)
    table.add_column("Check", style="cyan")
    table.add_column("Status")
    table.add_column("Actual")
    table.add_column("Expected")
    for check in result.checks:
        table.add_row(
            check.name,
            "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]",
            check.actual,
            check.expected,
        )
    return table


def _write_json_atomic(path: str, payload: dict):
    """Atomically rewrite a JSON config file using temp-file + rename.

    No external commands are run here. This writes a sibling temp file and then
    swaps it into place with `os.replace(...)` so `daemon.json` is never left in
    a partially written state.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path = target.with_suffix(target.suffix + ".lium.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    os.replace(temp_path, target)


def _update_fstab(fstab_path: str, uuid: str):
    """Insert or replace the `/var/lib/docker` mount entry in `/etc/fstab`.

    No external commands are run here. The function is intentionally limited to
    text-level editing:

    - preserve unrelated lines verbatim
    - replace the existing `/var/lib/docker` entry if one exists
    - avoid appending duplicate entries on repeated runs
    """
    target = Path(fstab_path)
    existing_lines = target.read_text().splitlines() if target.exists() else []
    mountpoint = "/var/lib/docker"
    desired = f"UUID={uuid} {mountpoint} xfs defaults,pquota 0 0"
    output = []
    replaced = False
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == mountpoint:
                if not replaced:
                    output.append(desired)
                    replaced = True
                continue
        output.append(line)
    if not replaced:
        output.append(desired)
    target.write_text("\n".join(output) + "\n")


def _fstab_would_change(fstab_path: str, uuid: str) -> bool:
    target = Path(fstab_path)
    desired = f"UUID={uuid} /var/lib/docker xfs defaults,pquota 0 0"
    if not target.exists():
        return True
    for line in target.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1] == "/var/lib/docker":
            return stripped != desired
    return True


def _get_uuid(path: str) -> str:
    """Resolve the filesystem UUID for one block device or partition.

    Command run:

    - `blkid -s UUID -o value <path>`
    """
    result = run_command(["blkid", "-s", "UUID", "-o", "value", path], True, True)
    uuid = result.stdout.strip()
    if not uuid:
        raise RuntimeError(f"Unable to determine UUID for {path}")
    return uuid


def _target_partition_path(plan: SetupPlan) -> str:
    if plan.target.created_partition_path:
        return plan.target.created_partition_path
    return plan.target.resolved_target


def _partition_paths_for_disk(parent_disk: str) -> set[str]:
    """Return the current partition paths for one disk.

    Command run:

    - `lsblk --json -o PATH,PKNAME,TYPE`

    This is used twice around `parted mkpart ...` so setup can identify the one
    newly created partition by diffing before/after snapshots instead of
    guessing from lexical sort order.
    """
    result = run_command(["lsblk", "--json", "-o", "PATH,PKNAME,TYPE"], True, True)
    data = json.loads(result.stdout or "{}")
    parent_name = os.path.basename(parent_disk)

    paths: set[str] = set()

    def walk(items):
        for item in items:
            if item.get("type") == "part" and item.get("pkname") == parent_name and item.get("path"):
                paths.add(item["path"])
            for child in item.get("children", []) or []:
                walk([child])

    walk(data.get("blockdevices", []))
    return paths


def _detect_new_partition(parent_disk: str, before_paths: set[str]) -> str:
    """Detect the single partition added to a disk after partitioning.

    This function does not run commands directly; it reuses
    `_partition_paths_for_disk(...)`, which runs `lsblk --json -o PATH,PKNAME,TYPE`.
    We require exactly one new path so the subsequent `mkfs.xfs` call cannot hit
    an unrelated pre-existing partition.
    """
    after_paths = _partition_paths_for_disk(parent_disk)
    new_paths = sorted(after_paths - before_paths)
    if len(new_paths) != 1:
        raise RuntimeError(f"Expected exactly one new partition on {parent_disk}, found {len(new_paths)}")
    return new_paths[0]


def _ensure_xfs_compliant(target_path: str):
    """Verify that a partition is XFS with `ftype=1`.

    Command run:

    - `xfs_info <target>`
    """
    result = run_command(["xfs_info", target_path], True, True)
    if "ftype=1" not in result.stdout and "ftype=1" not in result.stderr:
        raise RuntimeError(f"{target_path} is not XFS with ftype=1")


def _mount_validation(target_path: str, docker_root: str):
    """Perform a temporary mount probe before cutting over Docker root.

    Commands run here:

    - `mount -o pquota <target> /mnt/lium-docker-validate`
    - `findmnt --json /mnt/lium-docker-validate` via `load_mount_info(...)`
    - `umount /mnt/lium-docker-validate`

    The temporary mount checks two things before we touch `/var/lib/docker`:

    - the kernel accepted `pquota`
    - the mounted filesystem is writable
    """
    validation_mount = Path("/mnt/lium-docker-validate")
    validation_mount.mkdir(parents=True, exist_ok=True)
    run_command(["mount", "-o", "pquota", target_path, str(validation_mount)], True, True)
    try:
        mount_info = load_mount_info(run_command, str(validation_mount))
        if "pquota" not in mount_info.options:
            raise RuntimeError("Temporary mount validation failed: pquota not present")
        if not os.access(str(validation_mount), os.W_OK):
            raise RuntimeError("Temporary mount validation failed: mount is not writable")
    finally:
        run_command(["umount", str(validation_mount)], True, True)


def _print_action_details(plan: SetupPlan):
    console.print(_plan_table(plan))
    console.print(
        Panel(
            "\n".join(f"- {action}" for action in plan.destructive_actions),
            title="Destructive Actions",
            border_style="yellow",
        )
    )


def _confirm_plan(plan: SetupPlan, yes: bool):
    """Print the destructive plan and gate execution behind confirmation.

    No system commands run here. This is intentionally the last stop before the
    setup path starts issuing host-changing commands such as `systemctl stop`,
    `parted`, `mkfs.xfs`, and `mount`.
    """
    _print_action_details(plan)
    if yes:
        return
    if not Confirm.ask("Proceed?", default=False):
        raise click.Abort()


def _execute_setup(plan: SetupPlan):
    """Run the full host-mutation sequence for GPU-splitting storage setup.

    Commands run by phase:

    1. Stop Docker
       - `systemctl stop docker`
       - `systemctl stop docker.socket`

    2. Back up the current Docker root
       - `rsync -aHAX --numeric-ids <docker-root>/ <backup-dir>/`

    3. Prepare the target partition
       - optional `parted -s <disk> mklabel gpt`
       - optional `parted -s <disk> mkpart primary xfs <start> <end>`
       - optional `partprobe <disk>`
       - `lsblk --json -o PATH,PKNAME,TYPE` before/after via helper

    4. Format / validate filesystem metadata
       - optional `mkfs.xfs -f <partition>`
       - `xfs_info <partition>`

    5. Temporary mount validation
       - `mount -o pquota <partition> /mnt/lium-docker-validate`
       - `findmnt --json /mnt/lium-docker-validate` via helper
       - `umount /mnt/lium-docker-validate`

    6. Persist host configuration
       - rewrite `/etc/docker/daemon.json`
       - update `/etc/fstab`
       - `blkid -s UUID -o value <partition>`

    7. Cut over Docker root and restore data
       - optional `umount /var/lib/docker`
       - `mount /var/lib/docker`
       - `findmnt --json /var/lib/docker` via helper
       - `rsync -aHAX --numeric-ids <backup-dir>/ <docker-root>/`

    8. Restart and verify Docker
       - `systemctl start docker`
       - `docker info --format '{{json .}}'` via helper
       - `findmnt --json /var/lib/docker` via helper

    The sequence is intentionally linear and verbose because a partial failure
    here affects real host storage state.
    """
    total_steps = 10
    target_partition = _target_partition_path(plan)
    existing_partition_paths = _partition_paths_for_disk(plan.target.parent_disk)

    with timed_step_status(1, total_steps, "Stopping Docker"):
        console.dim(f"Stopping docker services for {plan.preflight.docker_state.docker_root_dir}")
        run_command(["systemctl", "stop", "docker"], True, True)
        run_command(["systemctl", "stop", "docker.socket"], False, True)

    with timed_step_status(2, total_steps, "Backing up Docker data"):
        console.dim(f"Backup directory: {plan.backup_dir}")
        Path(plan.backup_dir).mkdir(parents=True, exist_ok=True)
        run_command(
            ["rsync", "-aHAX", "--numeric-ids", f"{plan.preflight.docker_state.docker_root_dir}/", f"{plan.backup_dir}/"],
            True,
            True,
        )

    with timed_step_status(3, total_steps, "Preparing partition"):
        if plan.target.needs_new_gpt:
            console.dim(f"Creating GPT label on {plan.target.parent_disk}")
            run_command(["parted", "-s", plan.target.parent_disk, "mklabel", "gpt"], True, True)
        if plan.target.needs_partition_creation:
            if plan.target.free_region_start_bytes and plan.target.free_region_end_bytes:
                start = f"{plan.target.free_region_start_bytes}B"
                end = f"{plan.target.free_region_end_bytes}B"
            else:
                start = "1MiB"
                end = "100%"
            console.dim(f"Creating partition on {plan.target.parent_disk} from {start} to {end}")
            run_command(["parted", "-s", plan.target.parent_disk, "mkpart", "primary", "xfs", start, end], True, True)
            run_command(["partprobe", plan.target.parent_disk], False, True)
            plan.target.created_partition_path = _detect_new_partition(
                plan.target.parent_disk,
                existing_partition_paths,
            )
            target_partition = _target_partition_path(plan)
            console.dim(f"Created partition: {target_partition}")
        else:
            console.dim(f"Skipping partition creation for {target_partition}")

    with timed_step_status(4, total_steps, "Formatting XFS"):
        target_partition = _target_partition_path(plan)
        if plan.target.needs_format:
            console.dim(f"Formatting {target_partition} as XFS")
            run_command(["mkfs.xfs", "-f", target_partition], True, True)
        else:
            console.dim(f"Skipping format; existing filesystem will be reused: {target_partition}")
        _ensure_xfs_compliant(target_partition)

    with timed_step_status(5, total_steps, "Validating temporary mount"):
        _mount_validation(target_partition, plan.preflight.docker_state.docker_root_dir)

    with timed_step_status(6, total_steps, "Updating daemon.json"):
        console.dim(f"Writing Docker config to {plan.daemon_json_path}")
        _write_json_atomic(plan.daemon_json_path, plan.daemon_json_merged)

    with timed_step_status(7, total_steps, "Updating fstab"):
        uuid = _get_uuid(target_partition)
        console.dim(f"Using UUID={uuid} for /var/lib/docker")
        _update_fstab(plan.fstab_path, uuid)

    with timed_step_status(8, total_steps, "Cutting over /var/lib/docker"):
        docker_root = Path(plan.preflight.docker_state.docker_root_dir)
        docker_root.mkdir(parents=True, exist_ok=True)
        current_mount = load_mount_info(run_command, str(docker_root))
        if current_mount.target == str(docker_root) and current_mount.source != target_partition:
            run_command(["umount", str(docker_root)], False, True)
        run_command(["mount", str(docker_root)], True, True)
        current_mount = load_mount_info(run_command, str(docker_root))
        if "pquota" not in current_mount.options:
            raise RuntimeError("Mounted Docker root without pquota")

    with timed_step_status(9, total_steps, "Restoring Docker data"):
        console.dim(f"Restoring backup from {plan.backup_dir}")
        run_command(
            ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{plan.preflight.docker_state.docker_root_dir}/"],
            True,
            True,
        )

    with timed_step_status(10, total_steps, "Starting Docker and verifying"):
        run_command(["systemctl", "start", "docker"], True, True)
        verification = build_verification_result(run_command, plan.preflight.docker_state.docker_root_dir)
        console.print(_verification_table(verification, "Final Verification"))
        if not verification.passed:
            raise RuntimeError(f"Verification failed. Backup remains at {plan.backup_dir}")


@click.group("gpu-splitting")
def gpu_splitting_command():
    """Prepare Docker storage for LIUM GPU splitting."""


@gpu_splitting_command.command("check")
@click.option("--device", type=click.Path(path_type=Path))
@gpu_command_errors
def gpu_splitting_check_command(device: Optional[Path]):
    """Inspect the host and print the plan without making changes."""
    with timed_step_status(1, 2, "Running preflight inspection"):
        preflight = _preflight()
    with timed_step_status(2, 2, "Resolving target"):
        plan = _build_setup_plan(preflight, str(device) if device else None)

    console.print(_docker_state_table(preflight))
    console.print(
        _candidate_table(
            evaluate_candidates(
                run_command,
                preflight.block_devices,
                preflight.root_mount,
                preflight.docker_mount,
            )
        )
    )
    console.print(_plan_table(plan))
    console.print(
        Panel(
            "\n".join(f"- {action}" for action in plan.destructive_actions),
            title="What setup would do",
            border_style="cyan",
        )
    )


@gpu_splitting_command.command("verify")
@gpu_command_errors
def gpu_splitting_verify_command():
    """Verify the host matches LIUM GPU splitting requirements."""
    with timed_step_status(1, 1, "Running verification"):
        verification = build_verification_result(run_command)
    console.print(_verification_table(verification))
    if not verification.passed:
        raise RuntimeError("One or more GPU splitting requirements are not satisfied")


@gpu_splitting_command.command("setup")
@click.option("--device", type=click.Path(path_type=Path))
@click.option("--yes", is_flag=True, help="Skip interactive confirmation")
@gpu_command_errors
def gpu_splitting_setup_command(device: Optional[Path], yes: bool):
    """Perform end-to-end Docker storage setup for GPU splitting."""
    with timed_step_status(1, 2, "Running preflight inspection"):
        preflight = _preflight()
    with timed_step_status(2, 2, "Resolving target"):
        plan = _build_setup_plan(preflight, str(device) if device else None)

    try:
        _confirm_plan(plan, yes)
    except click.Abort:
        console.warning("Aborted before making changes")
        return

    _execute_setup(plan)
    console.success("GPU splitting setup completed successfully")
