"""Storage inspection and target planning helpers."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Optional

from .models import BlockDevice, CandidateEvaluation, CommandResult, MountInfo, TargetPlan

Runner = Callable[[list[str], bool, bool], CommandResult]


def run_json_command(runner: Runner, args: list[str]) -> dict:
    """Run a JSON-emitting command through the injected runner.

    Typical commands passed here:

    - `lsblk --json --bytes ...`
    - `findmnt --json --target /`
    - `findmnt --json --target /var/lib/docker`

    Keeping JSON parsing here avoids repeating the same decode/error handling in
    the storage-inspection helpers.
    """
    result = runner(args, True, True)
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {' '.join(args)}: {exc}") from exc


def _to_size_bytes(raw: str | int | None) -> int:
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw))
    except ValueError:
        return 0


def _normalize_mountpoints(item: dict) -> list[str]:
    mountpoints = item.get("mountpoints")
    if isinstance(mountpoints, list):
        return [value for value in mountpoints if value]
    mountpoint = item.get("mountpoint")
    return [mountpoint] if mountpoint else []


def _build_block_device(item: dict) -> BlockDevice:
    children = [_build_block_device(child) for child in item.get("children", [])]
    return BlockDevice(
        path=item.get("path") or f"/dev/{item.get('name')}",
        name=item.get("name", ""),
        kernel_name=item.get("kname") or item.get("name", ""),
        type=item.get("type", ""),
        size_bytes=_to_size_bytes(item.get("size")),
        fstype=item.get("fstype"),
        mountpoints=_normalize_mountpoints(item),
        parent_kernel_name=item.get("pkname"),
        model=item.get("model"),
        serial=item.get("serial"),
        is_removable=bool(item.get("rm")),
        children=children,
    )


def load_block_devices(runner: Runner) -> list[BlockDevice]:
    """Load block device inventory from `lsblk`.

    Command run:

    - `lsblk --json --bytes -o NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,FSAVAIL,FSUSE%,MOUNTPOINTS,PKNAME,MODEL,SERIAL,RM`

    The result preserves tree structure so callers can reason about both whole
    disks and their child partitions.
    """
    data = run_json_command(
        runner,
        [
            "lsblk",
            "--json",
            "--bytes",
            "-o",
            "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,FSAVAIL,FSUSE%,MOUNTPOINTS,PKNAME,MODEL,SERIAL,RM",
        ],
    )
    return [_build_block_device(item) for item in data.get("blockdevices", [])]


def flatten_devices(devices: list[BlockDevice]) -> list[BlockDevice]:
    """Flatten nested block devices."""
    flattened: list[BlockDevice] = []
    for device in devices:
        flattened.append(device)
        flattened.extend(flatten_devices(device.children))
    return flattened


def load_mount_info(runner: Runner, target: str) -> MountInfo:
    """Read mount metadata for a path or mountpoint.

    Command run:

    - `findmnt --json --target <target>`

    We use this for `/`, `/var/lib/docker`, and the temporary validation
    mountpoint so later safety checks can compare device sources, filesystems,
    and mount options such as project quota (`pquota`/`prjquota`).
    """
    data = run_json_command(runner, ["findmnt", "--json", "--target", target])
    filesystems = data.get("filesystems") or []
    if not filesystems:
        raise RuntimeError(f"Unable to determine mount info for {target}")
    item = filesystems[0]
    options = str(item.get("options", "")).split(",") if item.get("options") else []
    return MountInfo(
        target=item.get("target", target),
        source=item.get("source", ""),
        fstype=item.get("fstype", ""),
        options=[value for value in options if value],
    )


def has_project_quota_option(options: list[str]) -> bool:
    """Return true when mount options include XFS project quota.

    We treat both `pquota` and `prjquota` as valid aliases because kernels and
    tools may report either form.
    """
    for option in options:
        key = option.split("=", 1)[0].strip().lower()
        if key in {"pquota", "prjquota"}:
            return True
    return False


def get_device_by_path(devices: list[BlockDevice], path: str) -> Optional[BlockDevice]:
    """Return device matching its canonical path."""
    for device in flatten_devices(devices):
        if device.path == path:
            return device
    return None


def _root_disk_name(root_source: str) -> str:
    name = os.path.basename(root_source)
    if name.startswith("nvme") and "p" in name:
        return name.rsplit("p", 1)[0]
    if name[-1:].isdigit():
        return name.rstrip("0123456789")
    return name


def _is_protected_device(device: BlockDevice, root_mount: MountInfo, docker_mount: MountInfo) -> bool:
    root_source = root_mount.source
    docker_source = docker_mount.source
    if device.path in {root_source, docker_source}:
        return True

    parent_name = device.parent_kernel_name or device.kernel_name
    protected_names = {_root_disk_name(root_source)}
    if docker_source.startswith("/dev/"):
        protected_names.add(_root_disk_name(docker_source))
    return parent_name in protected_names


def _is_compliant_xfs(runner: Runner, path: str) -> bool:
    """Check whether a partition reports `ftype=1` in XFS metadata.

    Command run:

    - `xfs_info <path>`
    """
    result = runner(["xfs_info", path], False, True)
    text = (result.stdout or "") + (result.stderr or "")
    return "ftype=1" in text


def _partition_appears_to_contain_live_data(runner: Runner, path: str) -> tuple[bool, str]:
    """Read-only probe for obviously non-empty reusable XFS partitions.

    Commands run:

    - `mount -o ro,nouuid <path> <tempdir>`
    - `umount <tempdir>`

    The mount is read-only and temporary. If we cannot safely inspect the
    filesystem or if the mount contains any entries, we treat the partition as
    unsafe to reuse automatically.
    """
    with tempfile.TemporaryDirectory(prefix="lium-gpu-splitting-inspect-", dir="/tmp") as mount_dir:
        mount_result = runner(["mount", "-o", "ro,nouuid", path, mount_dir], False, True)
        if mount_result.returncode != 0:
            return True, "unable to inspect existing filesystem safely"
        try:
            entries = sorted(entry.name for entry in os.scandir(mount_dir))
        finally:
            runner(["umount", mount_dir], False, True)

    meaningful_entries = [entry for entry in entries if entry not in {".", ".."}]
    if meaningful_entries:
        preview = ", ".join(meaningful_entries[:3])
        return True, f"existing filesystem is not empty ({preview})"
    return False, "existing XFS filesystem is empty"


def _parse_parted_free_regions(stdout: str) -> list[tuple[int, int]]:
    free_regions: list[tuple[int, int]] = []
    for line in stdout.splitlines():
        if not line or line.startswith("BYT;"):
            continue
        parts = [part.strip() for part in line.split(":")]
        if len(parts) < 5 or parts[4] != "free":
            continue
        try:
            start = int(parts[1].rstrip("B"))
            end = int(parts[2].rstrip("B"))
        except ValueError:
            continue
        if end > start:
            free_regions.append((start, end))
    return free_regions


def inspect_free_regions(runner: Runner, disk_path: str) -> list[tuple[int, int]]:
    """Inspect free-space regions on a whole disk.

    Command run:

    - `parted -m <disk> unit B print free`

    The machine-readable `-m` output is parsed into `(start, end)` byte ranges
    so target selection can allow only the simple case of one unambiguous free
    region.
    """
    result = runner(["parted", "-m", disk_path, "unit", "B", "print", "free"], False, True)
    if result.returncode != 0:
        return []
    return _parse_parted_free_regions(result.stdout or "")


def _classify_partition(
    runner: Runner,
    device: BlockDevice,
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> tuple[str, bool]:
    if _is_protected_device(device, root_mount, docker_mount):
        return "protected_root_or_docker", False
    if device.mountpoints:
        return "unsupported_or_ambiguous", False
    if not device.fstype:
        return "unused_partition_no_fs", True
    if device.fstype == "xfs" and _is_compliant_xfs(runner, device.path):
        contains_data, _ = _partition_appears_to_contain_live_data(runner, device.path)
        if contains_data:
            return "unsupported_or_ambiguous", False
        return "unused_partition_xfs_compliant", True
    return "partition_existing_fs_reject", False


def _classify_disk(
    runner: Runner,
    device: BlockDevice,
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> tuple[str, bool, dict[str, Optional[int]]]:
    extra: dict[str, Optional[int]] = {"start": None, "end": None}
    if _is_protected_device(device, root_mount, docker_mount):
        return "protected_root_or_docker", False, extra
    if not device.children and not device.fstype and not device.mountpoints:
        return "blank_whole_disk", True, extra

    free_regions = inspect_free_regions(runner, device.path)
    if len(free_regions) == 1:
        start, end = free_regions[0]
        extra["start"] = start
        extra["end"] = end
        return "whole_disk_with_free_space", True, extra
    if len(free_regions) > 1:
        return "unsupported_or_ambiguous", False, extra
    return "unsupported_or_ambiguous", False, extra


def classify_target(
    runner: Runner,
    device: BlockDevice,
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> tuple[str, bool, dict[str, Optional[int]]]:
    """Classify one device into a setup-safe target type.

    This function does not launch commands on its own; it delegates to:

    - `_classify_partition(...)`, which may call `xfs_info` and temporary
      read-only `mount`/`umount`
    - `_classify_disk(...)`, which may call `parted -m ... print free`

    The return value is the core policy decision used by both `check` and
    `setup`.
    """
    if device.type == "part":
        classification, allowed = _classify_partition(runner, device, root_mount, docker_mount)
        return classification, allowed, {"start": None, "end": None}
    if device.type == "disk":
        return _classify_disk(runner, device, root_mount, docker_mount)
    return "unsupported_or_ambiguous", False, {"start": None, "end": None}


def explain_classification(
    runner: Runner,
    device: BlockDevice,
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> CandidateEvaluation:
    """Return a user-facing explanation for one candidate device.

    This wraps `classify_target(...)` and, for unmounted XFS partitions, may run
    the same read-only data probe (`mount -o ro,nouuid ...`, then `umount`) so
    the `check` command can explain why a candidate was rejected instead of just
    printing a generic classification label.
    """
    classification, allowed, _ = classify_target(runner, device, root_mount, docker_mount)
    details = classification
    if device.type == "part" and device.fstype == "xfs" and not device.mountpoints:
        contains_data, data_reason = _partition_appears_to_contain_live_data(runner, device.path)
        if contains_data:
            details = data_reason
    if classification == "protected_root_or_docker":
        details = "protected root or active Docker backing device"
    elif classification == "unused_partition_xfs_compliant":
        details = "empty XFS partition with ftype=1"
    elif classification == "unused_partition_no_fs":
        details = "unmounted partition without a filesystem"
    elif classification == "blank_whole_disk":
        details = "blank whole disk"
    elif classification == "whole_disk_with_free_space":
        details = "whole disk with one unambiguous free-space region"
    elif classification == "partition_existing_fs_reject":
        details = "existing non-XFS filesystem"
    elif classification == "unsupported_or_ambiguous" and device.is_removable:
        details = "removable or ambiguous device"
    return CandidateEvaluation(
        path=device.path,
        device_type=device.type,
        classification=classification,
        allowed=allowed,
        details=details,
    )


def evaluate_candidates(
    runner: Runner,
    devices: list[BlockDevice],
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> list[CandidateEvaluation]:
    """Evaluate all disk and partition candidates for `check`.

    No commands are launched directly here; each row delegates to
    `explain_classification(...)`, which in turn may invoke `parted`,
    `xfs_info`, or temporary read-only mount probes depending on device type.
    """
    evaluations = []
    for device in sorted(flatten_devices(devices), key=lambda item: item.path):
        if device.type not in {"disk", "part"}:
            continue
        if device.type == "loop":
            continue
        evaluations.append(explain_classification(runner, device, root_mount, docker_mount))
    return evaluations


def resolve_explicit_target(
    runner: Runner,
    devices: list[BlockDevice],
    root_mount: MountInfo,
    docker_mount: MountInfo,
    requested_device: str,
) -> TargetPlan:
    """Validate an operator-supplied `--device` without auto-fallback.

    Commands are delegated through `classify_target(...)`, which may run:

    - `parted -m <disk> unit B print free`
    - `xfs_info <partition>`
    - `mount -o ro,nouuid <partition> <tempdir>`
    - `umount <tempdir>`

    If the explicit device is unsafe, we fail immediately instead of choosing a
    different target behind the operator's back.
    """
    resolved = str(Path(requested_device).resolve(strict=True))
    if not resolved.startswith("/dev/"):
        raise RuntimeError(f"{requested_device} does not resolve to a device path under /dev")
    device = get_device_by_path(devices, resolved)
    if device is None:
        raise RuntimeError(f"Device not found in lsblk inventory: {resolved}")
    if device.type == "loop":
        raise RuntimeError(f"Loop devices are not supported: {resolved}")
    if "/dm-" in resolved or resolved.startswith("/dev/mapper/"):
        raise RuntimeError(f"Device-mapper targets are not supported in v1: {resolved}")

    classification, allowed, extra = classify_target(runner, device, root_mount, docker_mount)
    if not allowed:
        raise RuntimeError(f"Unsafe or unsupported device {resolved}: {classification}")

    return TargetPlan(
        selection_mode="explicit",
        requested_device=requested_device,
        resolved_target=resolved,
        parent_disk=_parent_disk_path(devices, device),
        classification=classification,
        needs_partition_creation=classification in {"blank_whole_disk", "whole_disk_with_free_space"},
        needs_format=classification in {"blank_whole_disk", "whole_disk_with_free_space", "unused_partition_no_fs"},
        existing_fs_type=device.fstype,
        selection_reason="Validated explicit device selection",
        needs_new_gpt=classification == "blank_whole_disk",
        free_region_start_bytes=extra.get("start"),
        free_region_end_bytes=extra.get("end"),
    )


def _parent_disk_path(devices: list[BlockDevice], device: BlockDevice) -> str:
    if device.type == "disk":
        return device.path
    if not device.parent_kernel_name:
        return device.path
    for item in flatten_devices(devices):
        if item.kernel_name == device.parent_kernel_name and item.type == "disk":
            return item.path
    return device.path


def auto_select_target(
    runner: Runner,
    devices: list[BlockDevice],
    root_mount: MountInfo,
    docker_mount: MountInfo,
) -> TargetPlan:
    """Select one safe device automatically or fail closed.

    This function does not issue commands directly; it repeatedly delegates to
    `classify_target(...)` for each disk/partition, so the effective inspection
    command set is:

    - `parted -m <disk> unit B print free`
    - `xfs_info <partition>`
    - `mount -o ro,nouuid <partition> <tempdir>`
    - `umount <tempdir>`

    Auto-selection is intentionally conservative:

    - prefer blank whole disks
    - then disks with exactly one free region
    - then empty partitions
    - then empty compliant XFS partitions
    - fail when multiple equally safe candidates exist
    """
    priorities = {
        "blank_whole_disk": 0,
        "whole_disk_with_free_space": 1,
        "unused_partition_no_fs": 2,
        "unused_partition_xfs_compliant": 3,
    }
    candidates: dict[int, list[TargetPlan]] = {value: [] for value in priorities.values()}

    for device in flatten_devices(devices):
        if device.type not in {"disk", "part"}:
            continue
        if device.type == "loop":
            continue
        classification, allowed, extra = classify_target(runner, device, root_mount, docker_mount)
        if not allowed or classification not in priorities:
            continue
        target = TargetPlan(
            selection_mode="auto",
            requested_device=None,
            resolved_target=device.path,
            parent_disk=_parent_disk_path(devices, device),
            classification=classification,
            needs_partition_creation=classification in {"blank_whole_disk", "whole_disk_with_free_space"},
            needs_format=classification in {"blank_whole_disk", "whole_disk_with_free_space", "unused_partition_no_fs"},
            existing_fs_type=device.fstype,
            selection_reason=f"Auto-selected {classification}",
            needs_new_gpt=classification == "blank_whole_disk",
            free_region_start_bytes=extra.get("start"),
            free_region_end_bytes=extra.get("end"),
        )
        candidates[priorities[classification]].append(target)

    for priority in sorted(candidates):
        items = sorted(
            [item for item in candidates[priority] if not get_device_by_path(devices, item.resolved_target).is_removable],
            key=lambda item: item.resolved_target,
        )
        if not items:
            items = sorted(candidates[priority], key=lambda item: item.resolved_target)
        if not items:
            continue
        if len(items) > 1:
            raise RuntimeError("Multiple equally safe storage targets were found; rerun with --device")
        return items[0]

    raise RuntimeError("No safe storage target found; rerun with --device or attach additional storage")
