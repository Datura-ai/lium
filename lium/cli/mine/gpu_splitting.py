"""GPU splitting setup commands."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISDIR, S_ISLNK
from typing import Callable, Optional

import click
from rich.box import SIMPLE_HEAVY
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ..utils import console, timed_step_status
from .docker_config import build_verification_result, inspect_docker_state, load_daemon_json, merge_overlay2
from .models import (
    DOCKER_DROP_IN_PATH,
    LOOP_DEVICE_UDEV_RULE_PATH,
    CandidateEvaluation,
    CommandResult,
    MountInfo,
    PreflightResult,
    SetupPlan,
    TargetPlan,
    VerificationResult,
)
from .storage import (
    MIN_DISK_TO_VRAM_RATE,
    MIN_TARGET_BYTES,
    NODE_FREE_DISK_GIB,
    NODE_IMAGES_GIB,
    NON_LOCAL_TRANSPORTS,
    NoSafeTargetError,
    auto_select_target,
    check_planned_loop_file_free_after_restore,
    disk_total_clears_vram_floor,
    evaluate_candidates,
    get_device_by_path,
    has_project_quota_option,
    load_block_devices,
    load_mounted_filesystems,
    load_mount_info,
    node_images_note,
    plan_loop_file_target,
    resolve_explicit_target,
)

GPU_SPLITTING_DOC_URL = "https://docs.lium.io/providers/nodes/gpu-splitting"
# fstab and the cutover mount the target at this path; another data-root would be left on the old disk
DEFAULT_DOCKER_ROOT = "/var/lib/docker"
# the image file is mounted by path through a loop device; direct I/O is set by the docker.service drop-in;
# nofail: without it a missing image file fails local-fs.target and the host boots into emergency mode
LOOP_FILE_FSTAB_OPTIONS = "loop,pquota,noatime,nofail"
# `lium mine --dir`'s default, relative to the working directory as there; its neurons/executor compose files name
# the node images, which count against the free-space floor only until they are pulled
DEFAULT_MINE_DIR = Path("compute-subnet")


@dataclass
class _FileSnapshot:
    path: str
    existed: bool
    contents: str


@dataclass
class _SetupExecutionState:
    original_daemon_json: _FileSnapshot
    original_fstab: _FileSnapshot
    original_docker_mount: MountInfo
    # both are set before their systemctl call: Docker is known to run before the stop, and may run once the start
    # was submitted, whatever an interrupt in between lets the caller record
    docker_restart_owed: bool = False
    docker_may_be_running: bool = False
    # set before `systemctl mask docker` at step 1: from the rename, or from the fstab line of step 7, a reboot would
    # start an enabled unit on an empty root; the rollback unmasks only with the data back and that line gone
    docker_masked: bool = False
    daemon_json_written: bool = False
    fstab_written: bool = False
    docker_root_mounted_on_target: bool = False
    original_docker_root_unmounted: bool = False
    original_drop_in: Optional[_FileSnapshot] = None
    drop_in_written: bool = False
    original_udev_rule: Optional[_FileSnapshot] = None
    udev_rule_written: bool = False
    docker_root_moved: bool = False
    loop_file_created: bool = False
    loop_file_removed: bool = False


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

    - `findmnt --json --canonicalize --evaluate --target /` via `load_mount_info(...)`
    - `docker info --format '{{json .}}'` via `inspect_docker_state(...)`
    - `findmnt --json --canonicalize --evaluate --target <docker-root>` via `load_mount_info(...)`
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
        "wipefs",
        "du",
        "losetup",
        "udevadm",
    ]:
        _require_binary(binary)

    run_command(["systemctl", "--version"], True, True)
    root_mount = load_mount_info(run_command, "/")
    docker_state = inspect_docker_state(run_command)
    if not docker_state.docker_info_available:
        reason = docker_state.docker_info_error or "docker info did not return server metadata"
        raise RuntimeError(
            "Unable to read Docker metadata required for gpu-splitting preflight.\n"
            f"Docker reported: {reason}\n"
            "Ensure the Docker daemon is running and run this command with root privileges."
        )
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


def _build_setup_plan(
    preflight: PreflightResult,
    device: Optional[str],
    image_file: Optional[str] = None,
    image_size_gib: Optional[int] = None,
    mine_dir: Optional[Path] = None,
) -> SetupPlan:
    """Build the full destructive plan without mutating the host.

    Commands run directly here:

    - `blkid -s UUID -o value <target>` when reusing an existing partition and
      we want to know whether `/etc/fstab` already matches

    Commands delegated through helpers:

    - explicit device validation / auto-selection from `storage.py`
    - `findmnt --json --bytes --list --real ...`, `nvidia-smi --query-gpu=memory.total ...` and
      `du -sxb <docker-root>` when the target is an XFS image file
    - `/etc/docker/daemon.json` load + merge from `docker_config.py`

    With no spare disk or partition (or with `--file` / `--size`) the target is an XFS
    image file on the local filesystem with the most free space, mounted through a loop
    device. The result is the exact plan that `check` prints and `setup` confirms before
    the first host mutation.
    """
    docker_root = preflight.docker_state.docker_root_dir
    if docker_root != DEFAULT_DOCKER_ROOT:
        raise RuntimeError(
            f"Docker's data-root is {docker_root}; setup supports only {DEFAULT_DOCKER_ROOT}. "
            f"Set the storage up by hand: {GPU_SPLITTING_DOC_URL}"
        )
    if device and (image_file or image_size_gib):
        raise RuntimeError("--device formats a disk and --file/--size create an image file; pass one of them")
    if device:
        target = resolve_explicit_target(
            run_command,
            preflight.block_devices,
            preflight.root_mount,
            preflight.docker_mount,
            device,
        )
    elif image_file or image_size_gib:
        target = _loop_file_target(preflight, image_file, image_size_gib, mine_dir)
    else:
        try:
            target = auto_select_target(
                run_command,
                preflight.block_devices,
                preflight.root_mount,
                preflight.docker_mount,
            )
        except NoSafeTargetError as exc:
            if preflight.docker_mount.target == docker_root:
                # an image file elsewhere would move Docker off its own disk and leave that disk unused
                raise RuntimeError(
                    f"{exc}. {docker_root} is its own disk ({preflight.docker_mount.source}); format a spare disk "
                    "with --device, or pass --file/--size to use an image file anyway"
                ) from exc
            target = _loop_file_target(preflight, None, None, mine_dir)

    loop_file = target.classification == "loop_file"
    if not loop_file:
        _check_target_device(preflight, target)

    daemon_json_path = "/etc/docker/daemon.json"
    existing_daemon = load_daemon_json(daemon_json_path)
    # the containerd image store reports its driver as `overlayfs`
    host_uses_containerd_image_store = preflight.docker_state.storage_driver == "overlayfs"
    merged_daemon, daemon_changed = merge_overlay2(existing_daemon, disable_containerd_snapshotter=host_uses_containerd_image_store)
    target_path = target.created_partition_path or target.resolved_target
    fstab_entry = ""
    if loop_file:
        fstab_entry = _fstab_entry(target_path, LOOP_FILE_FSTAB_OPTIONS)
    elif not target.needs_partition_creation:
        try:
            fstab_entry = _fstab_entry(f"UUID={_get_uuid(target_path)}", "defaults,pquota")
        except RuntimeError:
            fstab_entry = ""

    destructive_actions = _destructive_actions(preflight, target, loop_file)

    backup_dir = f"{docker_root}.lium-backup-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    return SetupPlan(
        preflight=preflight,
        target=target,
        backup_dir=backup_dir,
        daemon_json_path=daemon_json_path,
        daemon_json_changed=daemon_changed,
        daemon_json_merged=merged_daemon,
        fstab_path="/etc/fstab",
        fstab_changed=_fstab_would_change("/etc/fstab", fstab_entry) if fstab_entry else True,
        destructive_actions=destructive_actions,
    )


def _destructive_actions(preflight: PreflightResult, target: TargetPlan, loop_file: bool) -> list[str]:
    """The host changes `setup` makes, in order, as printed by `check` and confirmed by `setup`."""
    docker_root = preflight.docker_state.docker_root_dir
    daemon_json_path = "/etc/docker/daemon.json"
    host_uses_containerd_image_store = preflight.docker_state.storage_driver == "overlayfs"
    gib = 1024**3
    destructive_actions = []
    if loop_file:
        destructive_actions.append(
            f"create the XFS image file {target.resolved_target}: {target.loop_file_size_bytes // gib} GiB on "
            f"{target.loop_file_mountpoint}, {target.loop_file_reserve_bytes // gib} GiB stay free for the system"
        )
    if target.needs_new_gpt:
        destructive_actions.append(f"create GPT label on {target.parent_disk}")
    if target.needs_partition_creation:
        destructive_actions.append(f"create partition on {target.parent_disk}")
    if target.needs_format and not loop_file:
        destructive_actions.append(f"format target as XFS: {target.resolved_target}")
    destructive_actions.extend(
        [
            f"stop Docker services for {docker_root}",
            "move Docker data aside (same filesystem, no copy)" if _should_rename_docker_root_for_backup(preflight, target) else "back up Docker data",
            f"mount target at {docker_root} with pquota",
            f"update {daemon_json_path}",
            "update /etc/fstab",
        ]
    )
    if host_uses_containerd_image_store:
        destructive_actions.append(
            "switch Docker from the containerd image store to overlay2 (images and containers in the old store become "
            "unavailable: pull the images and recreate the containers afterwards)"
        )
    if loop_file:
        destructive_actions.append(f"add the docker.service drop-in {DOCKER_DROP_IN_PATH} (mount before Docker)")
        destructive_actions.append(
            f"add the udev rule {LOOP_DEVICE_UDEV_RULE_PATH} (direct I/O on the loop device, discard off so fstrim "
            "cannot punch holes in the file)"
        )
    destructive_actions.extend(["restore Docker data", "restart Docker"])
    return destructive_actions


def _loop_file_target(
    preflight: PreflightResult, image_file: Optional[str], image_size_gib: Optional[int], mine_dir: Optional[Path] = None
) -> TargetPlan:
    """Plan the XFS image file; `findmnt --json --bytes --list --real ...` via `load_mounted_filesystems(...)`,
    `nvidia-smi --query-gpu=memory.total ...` via `_total_gpu_vram_bytes()`, `du -sxb <docker-root>` via
    `_docker_data_bytes(...)` (that data is restored into the file, so it counts against the file's free space),
    `lium mine`'s node-image check on the checkout via `_node_images_present(...)` (pulled images are in that data)."""
    filesystems = load_mounted_filesystems(run_command, preflight.block_devices)
    total_vram_bytes = _total_gpu_vram_bytes()
    if total_vram_bytes is None:
        console.warning(
            f"GPU VRAM could not be read (nvidia-smi); the image file is not checked against the validator's "
            f"{MIN_DISK_TO_VRAM_RATE}x VRAM disk floor for the idle incentive"
        )
    return plan_loop_file_target(
        filesystems,
        preflight.docker_state.docker_root_dir,
        image_file,
        image_size_gib * 1024**3 if image_size_gib else None,
        total_vram_bytes,
        _docker_data_bytes(preflight.docker_state.docker_root_dir),
        # the switch to overlay2 hides images in the containerd store, so lium mine pulls them again
        preflight.docker_state.storage_driver != "overlayfs" and _node_images_present(mine_dir or DEFAULT_MINE_DIR),
    )


def _docker_data_bytes(docker_root: str) -> int:
    return int(run_command(["du", "-sxb", docker_root], True, True).stdout.split()[0])


def _node_images_present(mine_dir: Path) -> bool:
    # `lium mine`'s own rule (its free-disk check): every image the node runs is local, read from the checkout's
    # compose files; one stale executor image is not enough, and without the checkout they count as still to pull
    executor_dir = mine_dir / "neurons" / "executor"
    if not executor_dir.is_dir():
        console.warning(
            f"No `lium mine` checkout at {mine_dir}: the {NODE_IMAGES_GIB} GiB of node images count as still to be "
            "pulled; pass --mine-dir if the node already runs them"
        )
        return False
    # imported here as mine.py imports this module: inside the function, so neither import runs at load time
    from ..commands.mine import _node_images_present as node_images_present_in_checkout

    return node_images_present_in_checkout(executor_dir)


def _total_gpu_vram_bytes() -> Optional[int]:
    # `nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits`: one MiB figure per GPU; None when it
    # cannot be read, so the caller warns instead of blocking
    try:
        found = run_command(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"], False, True)
    except FileNotFoundError:
        return None
    if found.returncode != 0:
        return None
    try:
        return sum(int(line.strip()) for line in found.stdout.splitlines() if line.strip()) * 1024**2
    except ValueError:
        return None


def _should_rename_docker_root_for_backup(preflight: PreflightResult, target: TargetPlan) -> bool:
    # the image file shares the filesystem with Docker's data, so the data root is renamed aside, not copied;
    # a Docker root that is its own mount cannot be renamed off it and is copied as for a disk target
    return target.classification == "loop_file" and preflight.docker_mount.target != preflight.docker_state.docker_root_dir


def _target_disk(preflight: PreflightResult, target) -> tuple:
    device = get_device_by_path(preflight.block_devices, target.resolved_target)
    disk = get_device_by_path(preflight.block_devices, target.parent_disk) or device
    if device is None:
        raise RuntimeError(f"{target.resolved_target} is not in the lsblk inventory")
    return device, disk


def _target_size_bytes(device, target) -> int:
    if target.free_region_start_bytes is not None and target.free_region_end_bytes is not None:
        return target.free_region_end_bytes - target.free_region_start_bytes
    return device.size_bytes


def _check_target_device(preflight: PreflightResult, target):
    """Refuse a target that is removable, not local, too small or carries a signature lsblk did not name.

    Command run: `wipefs --no-act --noheadings -O TYPE <target>` for a blank disk or a partition without a filesystem.
    """
    device, disk = _target_disk(preflight, target)
    path = target.resolved_target
    if device.is_removable or disk.is_removable:
        raise RuntimeError(f"Refusing {path}: it is a removable disk")
    if (disk.transport or "").lower() in NON_LOCAL_TRANSPORTS:
        raise RuntimeError(f"Refusing {path}: it is attached over {disk.transport}, not a local disk")
    size = _target_size_bytes(device, target)
    if size < MIN_TARGET_BYTES:
        raise RuntimeError(f"Refusing {path}: {size // 1024**3} GiB is below the {MIN_TARGET_BYTES // 1024**3} GiB minimum")
    if target.classification in {"blank_whole_disk", "unused_partition_no_fs"}:
        found = run_command(["wipefs", "--no-act", "--noheadings", "-O", "TYPE", path], False, True)
        signatures = (found.stdout or "").split()
        if found.returncode != 0 or signatures:
            raise RuntimeError(
                f"Refusing {path}: it carries a signature lsblk did not report ({', '.join(signatures) or 'wipefs failed'}); "
                "if it really is spare, wipe it yourself and run setup again"
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
    if plan.target.classification == "loop_file":
        table.add_row("Image file on", plan.target.loop_file_mountpoint)
        table.add_row("Image file size", f"{plan.target.loop_file_size_bytes // 1024**3} GiB")
        table.add_row("Kept free for the system", f"{plan.target.loop_file_reserve_bytes // 1024**3} GiB")
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


def _print_gpu_splitting_requirements():
    console.print(
        Panel(
            "\n".join(
                [
                    "- Ubuntu/Debian Linux host with systemd",
                    "- Run `check` and `setup` as root (`sudo`)",
                    "- Docker daemon must be running and reachable",
                    (
                        "- Need one safe extra disk/partition (not root or current Docker backing device), or free space "
                        "on a local filesystem for an XFS image file (single-disk hosts)"
                    ),
                    "- Final required state: Docker `overlay2`, `/var/lib/docker` on XFS with project quota (`pquota`/`prjquota`), `Supports d_type=true`",
                    f"- Docs link: {GPU_SPLITTING_DOC_URL}",
                ]
            ),
            title="GPU Splitting Requirements",
            border_style="cyan",
        )
    )


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


def _snapshot_text_file(path: str) -> _FileSnapshot:
    target = Path(path)
    if not target.exists():
        return _FileSnapshot(path=path, existed=False, contents="")
    return _FileSnapshot(path=path, existed=True, contents=target.read_text())


def _restore_text_file(snapshot: _FileSnapshot):
    target = Path(snapshot.path)
    if snapshot.existed:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(snapshot.contents)
        return
    if target.exists():
        target.unlink()


def _fstab_entry(source: str, options: str) -> str:
    return f"{source} {DEFAULT_DOCKER_ROOT} xfs {options} 0 0"


def _update_fstab(fstab_path: str, desired: str):
    """Insert or replace the `/var/lib/docker` mount entry in `/etc/fstab`.

    No external commands are run here. The function is intentionally limited to
    text-level editing:

    - preserve unrelated lines verbatim
    - replace the existing `/var/lib/docker` entry if one exists
    - avoid appending duplicate entries on repeated runs
    """
    target = Path(fstab_path)
    existing_lines = target.read_text().splitlines() if target.exists() else []
    output = []
    replaced = False
    for line in existing_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            parts = stripped.split()
            if len(parts) >= 2 and parts[1] == DEFAULT_DOCKER_ROOT:
                if not replaced:
                    output.append(desired)
                    replaced = True
                continue
        output.append(line)
    if not replaced:
        output.append(desired)
    target.write_text("\n".join(output) + "\n")


def _fstab_would_change(fstab_path: str, desired: str) -> bool:
    target = Path(fstab_path)
    if not target.exists():
        return True
    for line in target.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parts = stripped.split()
        if len(parts) >= 2 and parts[1] == DEFAULT_DOCKER_ROOT:
            return stripped != desired
    return True


def _docker_drop_in_contents(docker_root: str) -> str:
    # without RequiresMountsFor Docker starts on the empty directory after a reboot and every pod "disappears"
    return f"[Unit]\nRequiresMountsFor={docker_root}\n"


def _loop_device_udev_rule(image_file: str) -> str:
    # applied on every attach, before the mount completes: direct I/O (without it pod creation is ~1.4x slower,
    # and fstab cannot set it) and no discard, so fstrim cannot punch holes back into the preallocated file
    return (
        'ACTION=="add|change", SUBSYSTEM=="block", KERNEL=="loop*", '
        f'ATTR{{loop/backing_file}}=="{image_file}", '
        'ATTR{queue/discard_max_bytes}="0", RUN+="/sbin/losetup --direct-io=on $devnode"\n'
    )


def _create_image_file(path: str, size_bytes: int, on_created: Callable[[], None] = lambda: None):
    # exclusive create with the final mode: no following a planted symlink, no formatting a file someone else
    # put there, no window in which the image is world-readable; `on_created` runs once the file is ours, so the
    # caller's rollback knows about it even when the preallocation is interrupted
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise RuntimeError(f"{path} appeared after the plan was made; remove it and run setup again") from None
    on_created()
    try:
        os.posix_fallocate(fd, 0, size_bytes)
    except BaseException:
        # a preallocation that stopped at ENOSPC or Ctrl+C keeps its extents; the file would leave the host full
        os.unlink(path)
        raise
    finally:
        os.close(fd)


def _same_filesystem(path_a: str, path_b: str) -> bool:
    return os.stat(path_a).st_dev == os.stat(path_b).st_dev


def _check_image_file_parent(image_file: str):
    # every directory from / down: O_EXCL guards the last component only, and the kernel reports the
    # loop's backing file by its resolved path, so a symlink or a user-owned ancestor breaks the udev rule
    parent = os.path.dirname(image_file)
    directory = "/"
    for component in parent.strip("/").split("/") if parent != "/" else []:
        directory = os.path.join(directory, component)
        stat = os.lstat(directory)
        if not S_ISDIR(stat.st_mode) or stat.st_uid != 0 or stat.st_mode & 0o022:
            raise RuntimeError(
                f"{directory} must be a directory owned by root and writable by root only (mode {stat.st_mode & 0o777:o}, "
                f"{'symlink' if S_ISLNK(stat.st_mode) else 'uid ' + str(stat.st_uid)}); the image file is reached through it. "
                "Pass --file with a path under /var/lib"
            )
    if os.path.lexists(image_file):
        raise RuntimeError(f"{image_file} already exists; remove it or choose another path")


def _write_sysfs(path: str, value: str):
    Path(path).write_text(value)


def _harden_loop_device(image_file: str):
    # the udev rule does this on every attach; applying it here too exercises the rule and covers an attach
    # whose uevent udev did not process
    name = run_command(["losetup", "-j", image_file, "-n", "-O", "NAME"], True, True).stdout.strip().splitlines()[0]
    run_command(["udevadm", "trigger", "--action=change", name], False, True)
    run_command(["udevadm", "settle"], False, True)
    _write_sysfs(f"/sys/block/{os.path.basename(name)}/queue/discard_max_bytes", "0")
    # the kernel refuses direct I/O when the backing disk has 4K logical sectors; it is only a speed option
    if run_command(["losetup", "--direct-io=on", name], False, True).returncode != 0:
        console.warning(f"Direct I/O could not be turned on for {name}; the image file works without it, a bit slower")


def _write_text_file(path: str, contents: str):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)


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


def _prepare_partition(plan: SetupPlan, existing_partition_paths: set[str]) -> str:
    """Create the planned partition (`parted mklabel` / `mkpart`, `partprobe`) and return the target path."""
    target_partition = _target_partition_path(plan)
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
    return target_partition


def _ensure_xfs_compliant(target_path: str):
    """Verify that a partition is XFS with `ftype=1`.

    Command run:

    - `xfs_info <target>`
    """
    result = run_command(["xfs_info", target_path], True, True)
    if "ftype=1" not in result.stdout and "ftype=1" not in result.stderr:
        raise RuntimeError(f"{target_path} is not XFS with ftype=1")


def _mount_validation(target_path: str, docker_root: str, loop_file: bool = False, total_vram_bytes: Optional[int] = None):
    """Perform a temporary mount probe before cutting over Docker root.

    Commands run here:

    - `mount -o pquota <target> /mnt/lium-docker-validate` (`-o loop,pquota` for an image file)
    - `findmnt --json --canonicalize --evaluate --target /mnt/lium-docker-validate` via `load_mount_info(...)`
    - `umount /mnt/lium-docker-validate`

    The temporary mount checks three things before we touch `/var/lib/docker`:

    - the kernel accepted project quota (`pquota` or `prjquota`)
    - the mounted filesystem is writable
    - its total (what the node will report) clears the validator's VRAM floor, when the VRAM is known
    """
    validation_mount = Path("/mnt/lium-docker-validate")
    validation_mount.mkdir(parents=True, exist_ok=True)
    options = "loop,pquota" if loop_file else "pquota"
    run_command(["mount", "-o", options, target_path, str(validation_mount)], True, True)
    try:
        mount_info = load_mount_info(run_command, str(validation_mount))
        if not has_project_quota_option(mount_info.options):
            raise RuntimeError("Temporary mount validation failed: project quota option not present (pquota/prjquota)")
        if not os.access(str(validation_mount), os.W_OK):
            raise RuntimeError("Temporary mount validation failed: mount is not writable")
        if total_vram_bytes is not None:
            _check_mounted_total_clears_vram_floor(str(validation_mount), total_vram_bytes)
    finally:
        run_command(["umount", str(validation_mount)], True, True)


def _check_mounted_total_clears_vram_floor(mountpoint: str, total_vram_bytes: int):
    # the total statfs reports is what the validator compares against the GPU VRAM, with its own rounding
    total = shutil.disk_usage(mountpoint).total
    gib = 1024**3
    if not disk_total_clears_vram_floor(total, total_vram_bytes):
        raise RuntimeError(
            f"The new filesystem reports {round(total / gib, 1)} GiB, under {MIN_DISK_TO_VRAM_RATE}x the "
            f"{round(total_vram_bytes / gib, 1)} GiB of GPU VRAM; the validator would pay no idle incentive. "
            "Rerun with a larger --size"
        )


def _check_mounted_root_free_after_restore(docker_root: str, free_floor_bytes: int):
    # the marketplace reads this free space; a file sized without the restored data would list the node and drop it
    free = shutil.disk_usage(docker_root).free
    gib = 1024**3
    if free < free_floor_bytes:
        raise RuntimeError(
            f"{free // gib} GiB free on {docker_root} after the restore; the marketplace lists a node only with "
            f"{NODE_FREE_DISK_GIB} GiB free {node_images_note(free_floor_bytes)} ({free_floor_bytes // gib} GiB needed)"
        )


def _print_action_details(plan: SetupPlan):
    console.print(_plan_table(plan))
    device = get_device_by_path(plan.preflight.block_devices, plan.target.parent_disk)
    if plan.target.classification == "loop_file":
        console.print(
            f"Image file {plan.target.resolved_target}: {plan.target.loop_file_size_bytes / 1024**3:.0f} GiB on "
            f"{plan.target.loop_file_mountpoint}, {plan.target.loop_file_reserve_bytes / 1024**3:.0f} GiB stay free "
            "for the system"
        )
    elif device is not None:
        console.print(
            f"Target disk {device.path}: {device.model or 'unknown model'}, serial {device.serial or 'unknown'}, "
            f"{device.size_bytes / 1024**3:.0f} GiB"
        )
    console.print(
        Panel(
            "\n".join(f"- {action}" for action in plan.destructive_actions),
            title="Destructive Actions",
            border_style="yellow",
        )
    )


def _confirm_plan(plan: SetupPlan, yes: bool, explicit_device: bool = False):
    """Print the destructive plan and gate execution behind confirmation.

    No system commands run here. This is intentionally the last stop before the
    setup path starts issuing host-changing commands such as `systemctl stop`,
    `parted`, `mkfs.xfs`, and `mount`. Consent is the operator typing the target's
    path at a terminal, or `--yes` with the disk named by `--device`. An image file
    formats nothing that exists, so `--yes` alone is enough for it.
    """
    _print_action_details(plan)
    loop_file = plan.target.classification == "loop_file"
    if yes:
        if not explicit_device and not loop_file:
            raise RuntimeError("--yes formats a disk without asking, so it needs --device naming that disk")
        return
    if not sys.stdin.isatty():
        console.warning("Confirming needs an interactive terminal (or --device <disk> --yes).")
        raise click.Abort()
    what = "image file path to create it" if loop_file else "target device path to format it"
    typed = Prompt.ask(f"Type the {what} ({plan.target.resolved_target})", default="")
    if typed.strip() != plan.target.resolved_target:
        raise click.Abort()


def _mount_command_for_mount_info(mount_info: MountInfo, target: str) -> list[str]:
    args = ["mount"]
    if mount_info.fstype:
        args.extend(["-t", mount_info.fstype])
    options = ",".join(option for option in mount_info.options if option)
    if options:
        args.extend(["-o", options])
    args.extend([mount_info.source, target])
    return args


def _rollback_attempt(errors: list[str], label: str, action) -> bool:
    try:
        action()
    except Exception as exc:  # pragma: no cover - exercised via callers
        errors.append(f"{label}: {exc}")
        return False
    return True


def _rollback_setup(plan: SetupPlan, state: _SetupExecutionState, target_partition: str) -> list[str]:
    # undo in reverse order; the image file is removed and Docker started only once the mount is gone and the
    # data root is back, otherwise a failed step would end with Docker on an empty directory or a nameless image
    docker_root = plan.preflight.docker_state.docker_root_dir
    errors: list[str] = []

    if state.docker_may_be_running:
        # `systemctl stop` of an inactive unit is a no-op, so no need to ask first
        _rollback_attempt(errors, "stop docker", lambda: run_command(["systemctl", "stop", "docker"], True, True))
        _rollback_attempt(errors, "stop docker.socket", lambda: run_command(["systemctl", "stop", "docker.socket"], False, True))

    # the flags are set after each helper returns, so an interrupt in between leaves the host changed and the flag
    # off: the mount table and the backup path decide too. A renamed-away root has no mount table entry to read.
    original = state.original_docker_mount
    root_exists = os.path.lexists(docker_root)
    current = _mount_at_or_none(docker_root) if root_exists else None
    mount_table_unknown = root_exists and current is None
    source_changed = current is not None and current.source != original.source
    mounted_on_target = state.docker_root_mounted_on_target or (source_changed and current.target == docker_root)
    original_unmounted = state.original_docker_root_unmounted or (source_changed and original.target == docker_root)
    rename_backup = _should_rename_docker_root_for_backup(plan.preflight, plan.target)
    # a backup path that exists while the root is gone is our interrupted `mv`; one beside a live root is not ours
    root_moved = state.docker_root_moved or (
        rename_backup and os.path.isdir(plan.backup_dir) and not os.path.lexists(docker_root)
    )

    # data_restored: the original data root is back under docker_root, whichever way the backup was taken
    unmounted = data_restored = not mount_table_unknown
    if mount_table_unknown:
        errors.append(
            f"read the mount table: `findmnt {docker_root}` failed, so whether {original.source} or {target_partition} "
            f"is mounted there is unknown; check `findmnt {docker_root}` by hand before starting Docker"
        )
    if mounted_on_target:
        unmounted = _rollback_attempt(errors, f"unmount {docker_root}", lambda: run_command(["umount", docker_root], True, True))
        if not unmounted:
            data_restored = False
    if unmounted and original_unmounted:
        data_restored = _rollback_attempt(
            errors,
            f"remount original Docker root {original.source}",
            lambda: run_command(_mount_command_for_mount_info(original, docker_root), True, True),
        )

    if root_moved:
        if unmounted:
            _rollback_attempt(errors, f"move Docker data back to {docker_root}", lambda: _move_docker_root_back(plan))
        else:
            errors.append(f"move Docker data back to {docker_root}: skipped, {docker_root} is still mounted")
    if rename_backup and (not os.path.isdir(docker_root) or (root_moved and os.path.lexists(plan.backup_dir))):
        # Docker starts only on a data root that is demonstrably back in place, never on an empty directory
        data_restored = False
    if data_restored and original.target == docker_root:
        # the rsync branch starts Docker only once the mount table shows the original filesystem back at the root
        back = _mount_at_or_none(docker_root)
        if back is None or back.source != original.source:
            data_restored = False
            errors.append(
                f"confirm {original.source} is mounted at {docker_root}: "
                + ("`findmnt` failed" if back is None else f"{back.source} is mounted there")
            )

    fstab_restored = True
    if state.fstab_written:
        fstab_restored = _rollback_attempt(errors, f"restore {plan.fstab_path}", lambda: _restore_text_file(state.original_fstab))

    if state.drop_in_written:
        _rollback_attempt(errors, f"restore {plan.drop_in_path}", lambda: _restore_text_file(state.original_drop_in))
        _rollback_attempt(errors, "systemctl daemon-reload", lambda: run_command(["systemctl", "daemon-reload"], False, True))

    if state.udev_rule_written:
        _rollback_attempt(errors, f"restore {plan.udev_rule_path}", lambda: _restore_text_file(state.original_udev_rule))
        _rollback_attempt(errors, "udevadm control --reload-rules", lambda: run_command(["udevadm", "control", "--reload-rules"], False, True))

    if state.daemon_json_written:
        _rollback_attempt(errors, f"restore {plan.daemon_json_path}", lambda: _restore_text_file(state.original_daemon_json))

    if state.loop_file_created:
        if not unmounted:
            errors.append(f"remove {target_partition}: skipped, still mounted at {docker_root}")
        elif not os.path.lexists(target_partition):
            state.loop_file_removed = True   # an interrupted preallocation has already unlinked the file itself
        else:
            # `unmounted` covers the Docker root only; the validation mount's umount can have failed, and a backing
            # file unlinked while attached keeps its space with no name to find it by
            attached = _loop_devices_backed_by(target_partition)
            if attached is None or attached:
                held = (
                    f"still attached to {', '.join(attached)}" if attached
                    else "`losetup -j` failed, so whether a loop device still holds it is unknown"
                )
                errors.append(f"remove {target_partition}: skipped, {held}; detach it before removing the file")
            else:
                state.loop_file_removed = _rollback_attempt(errors, f"remove {target_partition}", lambda: os.unlink(target_partition))

    if state.docker_restart_owed:
        # Docker may come back only with its data under docker_root and no fstab line left to mount the new
        # filesystem over it at boot; otherwise it is masked again (step 10 may have unmasked before the failure)
        if data_restored and fstab_restored:
            if state.docker_masked:
                _rollback_attempt(errors, "unmask docker", lambda: run_command(["systemctl", "unmask", "docker"], True, True))
            _rollback_attempt(errors, "start docker", lambda: run_command(["systemctl", "start", "docker"], True, True))
        else:
            _rollback_attempt(errors, "mask docker", lambda: run_command(["systemctl", "mask", "docker"], True, True))
            why = (
                f"Docker data is not back at {docker_root}" if not data_restored
                else f"{plan.fstab_path} would still mount {target_partition} over {docker_root} at boot"
            )
            errors.append(
                f"start docker: skipped, {why} (backup at {plan.backup_dir}); docker.service stays masked, so a reboot "
                f"does not start Docker on an empty root. Once the data is back at {docker_root} and the fstab line is "
                "gone: systemctl unmask docker && systemctl start docker"
            )

    return errors


def _loop_devices_backed_by(image_file: str) -> Optional[list[str]]:
    # None when `losetup -j` failed or is missing: the rollback must not unlink a file it cannot prove detached, and
    # an exception here would end the rollback before Docker is unmasked and started
    try:
        found = run_command(["losetup", "-j", image_file, "-n", "-O", "NAME"], False, True)
    except Exception:
        return None
    return None if found.returncode != 0 else found.stdout.split()


def _mount_at_or_none(docker_root: str) -> Optional[MountInfo]:
    try:
        return load_mount_info(run_command, docker_root)
    except (RuntimeError, OSError, ValueError):
        return None


def _refuse_mounts_under(docker_root: str):
    # `mv` would carry a mount left under the data root into the backup, and the final rmtree would erase that disk
    prefix = docker_root.rstrip("/") + "/"
    targets = run_command(["findmnt", "--list", "--noheadings", "-o", "TARGET"], True, True).stdout.split()
    mounted = [target for target in targets if target.startswith(prefix)]
    if mounted:
        raise RuntimeError(
            f"Something is still mounted under {docker_root}: {', '.join(mounted)}. Moving the data root aside would "
            "take the mount with it; unmount it (`umount <path>`) and run setup again"
        )


def _move_docker_root_back(plan: SetupPlan):
    # rmdir first: were the directory not empty, `mv` would put the backup inside it instead of in its place
    docker_root = plan.preflight.docker_state.docker_root_dir
    if os.path.isdir(docker_root):  # recreated by the cutover; absent when an earlier step failed
        run_command(["rmdir", docker_root], True, True)
    run_command(["mv", plan.backup_dir, docker_root], True, True)


def _irreversible_note(plan: SetupPlan, state: _SetupExecutionState) -> str:
    if plan.target.classification == "loop_file":
        if state.loop_file_removed:
            return "The XFS image file was removed."
        if state.loop_file_created:
            return f"The XFS image file {plan.target.resolved_target} was kept; see the rollback errors before removing it."
        return "No XFS image file was created."
    return "Disk partitioning and filesystem formatting were not rolled back."


def _setup_failure_message(plan: SetupPlan, state: _SetupExecutionState, exc: BaseException, rollback_errors: list[str]) -> str:
    lines = [
        f"GPU splitting setup failed: {str(exc) or type(exc).__name__}",
        "Rollback of reversible host changes was attempted.",
        _irreversible_note(plan, state),
        f"Backup remains at {plan.backup_dir}",
    ]
    if rollback_errors:
        lines.append("Rollback encountered additional errors:")
        lines.extend(f"- {error}" for error in rollback_errors)
    return "\n".join(lines)


def _pre_stop_checks(plan: SetupPlan):
    """Refuse before the first host change: Docker down, containers running, containers left in the containerd
    store, no room for the backup, an image file directory other users can write to, or Docker data that would
    leave the planned image file under the listing floor (the data may have grown since the plan).

    Commands run: `docker ps -q`, `du -sxb <docker-root>`.
    """
    ps = run_command(["docker", "ps", "-q"], False, True)
    if ps.returncode != 0:
        reason = (ps.stderr or "").strip() or "docker ps failed"
        raise RuntimeError(f"Docker is not responding ({reason}); start it with `systemctl start docker` and run setup again")
    if (ps.stdout or "").strip():
        raise RuntimeError("Containers are running on this host; move Docker storage only while it runs none (no rentals)")
    if plan.preflight.docker_state.storage_driver == "overlayfs":
        # the switch away from the containerd image store leaves its containers invisible to Docker
        existing = run_command(["docker", "ps", "-aq"], True, True).stdout.split()
        if existing:
            raise RuntimeError(
                f"{len(existing)} containers exist in Docker's containerd image store; after the switch to overlay2 "
                "Docker would no longer see them. Copy what you need out of them first (`docker save` keeps images "
                "only, not a container's own files), remove them (`docker rm`) and run setup again"
            )
    docker_root = plan.preflight.docker_state.docker_root_dir
    used = _docker_data_bytes(docker_root)
    if plan.target.classification == "loop_file":
        _check_image_file_parent(plan.target.resolved_target)
        check_planned_loop_file_free_after_restore(
            f"The {plan.target.loop_file_size_bytes // 1024**3} GiB image file {plan.target.resolved_target} would",
            plan.target.loop_file_size_bytes, used, docker_root, plan.target.loop_file_free_floor_bytes,
        )
    if _should_rename_docker_root_for_backup(plan.preflight, plan.target):
        # the data root is renamed, not copied, so the host needs no room for a backup
        return
    needed = int(used * 1.1) + 1024**3
    what = f"The backup of {docker_root}"
    backup_parent = str(Path(plan.backup_dir).parent)
    if plan.target.classification == "loop_file" and _same_filesystem(backup_parent, plan.target.loop_file_mountpoint):
        # the copy and the image file exist side by side until the restore step
        needed += plan.target.loop_file_size_bytes
        what += " and the image file"
    free = shutil.disk_usage(backup_parent).free
    if free < needed:
        raise RuntimeError(
            f"{what} needs {needed / 1024**3:.1f} GiB free next to it, "
            f"only {free / 1024**3:.1f} GiB is; free space and run setup again"
        )


def _execute_setup(plan: SetupPlan):
    """Run the full host-mutation sequence for GPU-splitting storage setup.

    Commands run by phase:

    1. Stop Docker
       - `systemctl stop docker`
       - `systemctl stop docker.socket`
       - `systemctl mask docker` until the start at step 8: a reboot in between must not start Docker on the
         renamed-away root or on the new filesystem that fstab names from step 6 on, before the data is on it

    2. Back up the current Docker root
       - `rsync -aHAX --numeric-ids <docker-root>/ <backup-dir>/`
       - or, for an image file on the same filesystem, `findmnt --list --noheadings -o TARGET` (no mount may
         be left under the data root) and `mv <docker-root> <backup-dir>`

    3. Prepare the target partition
       - optional `parted -s <disk> mklabel gpt`
       - optional `parted -s <disk> mkpart primary xfs <start> <end>`
       - optional `partprobe <disk>`
       - `lsblk --json -o PATH,PKNAME,TYPE` before/after via helper
       - or an exclusive create + `posix_fallocate` of the image file (mode 0600); the backup path must not exist

    4. Format / validate filesystem metadata
       - optional `mkfs.xfs -f <partition>` (`mkfs.xfs -f -K <image-file>`)
       - `xfs_info <partition>`

    5. Temporary mount validation
       - `mount -o pquota <partition> /mnt/lium-docker-validate`
       - `findmnt --json --canonicalize --evaluate --target /mnt/lium-docker-validate` via helper
       - statfs of the mount: its total against the validator's VRAM floor (image file with known VRAM)
       - `umount /mnt/lium-docker-validate`

    6. Persist host configuration
       - rewrite `/etc/docker/daemon.json`
       - update `/etc/fstab`
       - `blkid -s UUID -o value <partition>`
       - or, for an image file, the docker.service drop-in + `systemctl daemon-reload` and the udev rule +
         `udevadm control --reload-rules`; after the mount `udevadm trigger`, discard off, `losetup --direct-io=on`

    7. Cut over Docker root and restore data
       - optional `umount /var/lib/docker`
       - `mount /var/lib/docker`
       - `findmnt --json --canonicalize --evaluate --target /var/lib/docker` via helper
       - `rsync -aHAX --numeric-ids <backup-dir>/ <docker-root>/`

    8. Restart and verify Docker
       - `systemctl unmask docker`
       - `systemctl start docker`
       - `docker info --format '{{json .}}'` via helper
       - `findmnt --json --canonicalize --evaluate --target /var/lib/docker` via helper
       - statfs of the Docker root: free space against the listing floor (image file)

    9. Commit point: remove the backup. A failure or Ctrl+C here keeps the verified image, mount and config.

    The sequence is intentionally linear and verbose because a partial failure
    here affects real host storage state.
    """
    _pre_stop_checks(plan)
    total_steps = 10
    loop_file = plan.target.classification == "loop_file"
    rename_backup = _should_rename_docker_root_for_backup(plan.preflight, plan.target)
    docker_root = plan.preflight.docker_state.docker_root_dir
    target_partition = _target_partition_path(plan)
    existing_partition_paths = set() if loop_file else _partition_paths_for_disk(plan.target.parent_disk)
    state = _SetupExecutionState(
        original_daemon_json=_snapshot_text_file(plan.daemon_json_path),
        original_fstab=_snapshot_text_file(plan.fstab_path),
        original_docker_mount=plan.preflight.docker_mount,
        original_drop_in=_snapshot_text_file(plan.drop_in_path),
        original_udev_rule=_snapshot_text_file(plan.udev_rule_path),
    )

    try:
        with timed_step_status(1, total_steps, "Stopping Docker"):
            console.dim(f"Stopping docker services for {docker_root}")
            state.docker_restart_owed = True
            run_command(["systemctl", "stop", "docker"], True, True)
            run_command(["systemctl", "stop", "docker.socket"], False, True)
            state.docker_masked = True
            run_command(["systemctl", "mask", "docker"], True, True)

        with timed_step_status(2, total_steps, "Moving Docker data aside" if rename_backup else "Backing up Docker data"):
            console.dim(f"Backup directory: {plan.backup_dir}")
            if rename_backup:
                _refuse_mounts_under(docker_root)
                if os.path.lexists(plan.backup_dir):
                    # `mv` into an existing directory would nest the data root inside it
                    raise RuntimeError(f"{plan.backup_dir} already exists; remove it and run setup again")
                run_command(["mv", docker_root, plan.backup_dir], True, True)
                state.docker_root_moved = True
            else:
                Path(plan.backup_dir).mkdir(parents=True, exist_ok=True)
                run_command(["rsync", "-aHAX", "--numeric-ids", f"{docker_root}/", f"{plan.backup_dir}/"], True, True)

        with timed_step_status(3, total_steps, "Creating the image file" if loop_file else "Preparing partition"):
            if loop_file:
                console.dim(f"Preallocating {plan.target.loop_file_size_bytes // 1024**3} GiB at {target_partition}")
                _create_image_file(
                    target_partition, plan.target.loop_file_size_bytes, lambda: setattr(state, "loop_file_created", True)
                )
            else:
                target_partition = _prepare_partition(plan, existing_partition_paths)

        with timed_step_status(4, total_steps, "Formatting XFS"):
            target_partition = _target_partition_path(plan)
            if loop_file:
                console.dim(f"Formatting {target_partition} as XFS")
                # -K: mkfs discards blocks by default, and on a file the discard punches holes in what fallocate reserved
                run_command(["mkfs.xfs", "-f", "-K", target_partition], True, True)
            elif plan.target.needs_format:
                console.dim(f"Formatting {target_partition} as XFS")
                run_command(["mkfs.xfs", "-f", target_partition], True, True)
            else:
                console.dim(f"Skipping format; existing filesystem will be reused: {target_partition}")
            _ensure_xfs_compliant(target_partition)

        with timed_step_status(5, total_steps, "Validating temporary mount"):
            _mount_validation(target_partition, docker_root, loop_file, plan.target.loop_file_total_vram_bytes)

        # each `*_written` flag is set before its writer: an interrupt after the write but before the flag would
        # otherwise leave the file changed and the rollback blind to it; the snapshots were taken before the first step
        with timed_step_status(6, total_steps, "Updating daemon.json"):
            console.dim(f"Writing Docker config to {plan.daemon_json_path}")
            state.daemon_json_written = True
            _write_json_atomic(plan.daemon_json_path, plan.daemon_json_merged)

        with timed_step_status(7, total_steps, "Updating fstab, docker.service and udev" if loop_file else "Updating fstab"):
            if loop_file:
                console.dim(f"Mounting {target_partition} at {docker_root} with {LOOP_FILE_FSTAB_OPTIONS}")
                state.fstab_written = True
                _update_fstab(plan.fstab_path, _fstab_entry(target_partition, LOOP_FILE_FSTAB_OPTIONS))
                console.dim(f"Writing {plan.drop_in_path}")
                state.drop_in_written = True
                _write_text_file(plan.drop_in_path, _docker_drop_in_contents(docker_root))
                run_command(["systemctl", "daemon-reload"], True, True)
                console.dim(f"Writing {plan.udev_rule_path}")
                state.udev_rule_written = True
                _write_text_file(plan.udev_rule_path, _loop_device_udev_rule(target_partition))
                run_command(["udevadm", "control", "--reload-rules"], True, True)
            else:
                uuid = _get_uuid(target_partition)
                console.dim(f"Using UUID={uuid} for /var/lib/docker")
                state.fstab_written = True
                _update_fstab(plan.fstab_path, _fstab_entry(f"UUID={uuid}", "defaults,pquota"))

        with timed_step_status(8, total_steps, "Cutting over /var/lib/docker"):
            docker_root = Path(plan.preflight.docker_state.docker_root_dir)
            docker_root.mkdir(parents=True, exist_ok=True)
            current_mount = load_mount_info(run_command, str(docker_root))
            if current_mount.target == str(docker_root) and current_mount.source != target_partition:
                state.original_docker_root_unmounted = run_command(["umount", str(docker_root)], False, True).returncode == 0
            run_command(["mount", str(docker_root)], True, True)
            state.docker_root_mounted_on_target = True
            current_mount = load_mount_info(run_command, str(docker_root))
            if not has_project_quota_option(current_mount.options):
                raise RuntimeError("Mounted Docker root without project quota option (pquota/prjquota)")
            if loop_file:
                _harden_loop_device(target_partition)

        with timed_step_status(9, total_steps, "Restoring Docker data"):
            console.dim(f"Restoring backup from {plan.backup_dir}")
            run_command(
                ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{plan.preflight.docker_state.docker_root_dir}/"],
                True,
                True,
            )

        with timed_step_status(10, total_steps, "Starting Docker and verifying"):
            if state.docker_masked:
                run_command(["systemctl", "unmask", "docker"], True, True)
                state.docker_masked = False
            state.docker_may_be_running = True
            run_command(["systemctl", "start", "docker"], True, True)
            verification = build_verification_result(run_command, plan.preflight.docker_state.docker_root_dir)
            console.print(_verification_table(verification, "Final Verification"))
            if not verification.passed:
                raise RuntimeError(f"Verification failed. Backup remains at {plan.backup_dir}")
            if loop_file:
                _check_mounted_root_free_after_restore(str(docker_root), plan.target.loop_file_free_floor_bytes)
    except (Exception, KeyboardInterrupt) as exc:
        # Ctrl+C included: without the rollback the host stays with Docker stopped and its data root moved aside
        console.error(f"Setup failed: {str(exc) or type(exc).__name__}")
        console.warning("Attempting rollback of reversible host changes.")
        console.warning(f"Backup remains at {plan.backup_dir}")
        rollback_errors = _rollback_setup(plan, state, target_partition)
        console.warning(_irreversible_note(plan, state))
        if rollback_errors:
            console.error("Rollback encountered additional errors.")
        else:
            console.success("Reversible host changes rolled back and Docker was restarted.")
        if isinstance(exc, KeyboardInterrupt):
            # the interrupt itself goes on, so `lium mine` stops instead of treating it as a refused storage step
            console.error(_setup_failure_message(plan, state, exc, rollback_errors))
            raise
        raise RuntimeError(_setup_failure_message(plan, state, exc, rollback_errors)) from exc

    # commit point: the data is restored and verified on the new mount, the backup only holds the old disk's space;
    # a cleanup that fails or is interrupted must never roll the migration back over a half-deleted backup
    try:
        shutil.rmtree(plan.backup_dir)
    except KeyboardInterrupt:
        console.warning(f"Setup is complete; the backup at {plan.backup_dir} was only partly removed, delete it: rm -rf {plan.backup_dir}")
        raise
    except OSError as exc:
        console.warning(f"Setup is complete; the backup at {plan.backup_dir} could not be fully removed ({exc}), delete it: rm -rf {plan.backup_dir}")


@click.group("gpu-splitting")
def gpu_splitting_command():
    """Prepare Docker storage for LIUM GPU splitting."""


_IMAGE_FILE_OPTION = click.option(
    "--file", "image_file", type=click.Path(path_type=Path),
    help="Create the XFS image file at this path instead of formatting a disk (single-disk hosts)",
)
_IMAGE_SIZE_OPTION = click.option(
    "--size", "image_size_gib", type=int,
    help=f"Size of the XFS image file in GiB (default: free space minus the system reserve; it must keep "
    f"{NODE_FREE_DISK_GIB} GiB free after the Docker data and the node images, and clear {MIN_DISK_TO_VRAM_RATE}x the "
    "host's GPU VRAM)",
)
_MINE_DIR_OPTION = click.option(
    "--mine-dir", type=click.Path(path_type=Path),
    help=f"The directory `lium mine --dir` used (default: {DEFAULT_MINE_DIR}); node images already pulled there are "
    "not added to the free-space floor again",
)


@gpu_splitting_command.command("check")
@click.option("--device", type=click.Path(path_type=Path))
@_IMAGE_FILE_OPTION
@_IMAGE_SIZE_OPTION
@_MINE_DIR_OPTION
@gpu_command_errors
def gpu_splitting_check_command(
    device: Optional[Path], image_file: Optional[Path], image_size_gib: Optional[int], mine_dir: Optional[Path]
):
    """Inspect the host and print the plan without making changes."""
    _print_gpu_splitting_requirements()
    with timed_step_status(1, 2, "Running preflight inspection"):
        preflight = _preflight()
    with timed_step_status(2, 2, "Resolving target"):
        plan = _build_setup_plan(
            preflight, str(device) if device else None, str(image_file) if image_file else None, image_size_gib, mine_dir
        )

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
    _print_gpu_splitting_requirements()
    with timed_step_status(1, 1, "Running verification"):
        verification = build_verification_result(run_command)
    console.print(_verification_table(verification))
    if not verification.passed:
        raise RuntimeError("One or more GPU splitting requirements are not satisfied")


@gpu_splitting_command.command("setup")
@click.option("--device", type=click.Path(path_type=Path))
@_IMAGE_FILE_OPTION
@_IMAGE_SIZE_OPTION
@_MINE_DIR_OPTION
@click.option("--yes", is_flag=True, help="Skip interactive confirmation")
@gpu_command_errors
def gpu_splitting_setup_command(
    device: Optional[Path], image_file: Optional[Path], image_size_gib: Optional[int], mine_dir: Optional[Path], yes: bool
):
    """Perform end-to-end Docker storage setup for GPU splitting."""
    _print_gpu_splitting_requirements()
    with timed_step_status(1, 2, "Running preflight inspection"):
        preflight = _preflight()
    with timed_step_status(2, 2, "Resolving target"):
        plan = _build_setup_plan(
            preflight, str(device) if device else None, str(image_file) if image_file else None, image_size_gib, mine_dir
        )

    try:
        _confirm_plan(plan, yes, explicit_device=device is not None)
    except click.Abort:
        console.warning("Aborted before making changes")
        return

    _execute_setup(plan)
    console.success("GPU splitting setup completed successfully")
