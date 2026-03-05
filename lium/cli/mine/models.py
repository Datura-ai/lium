"""Typed models for mine command workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class CommandResult:
    """Structured subprocess result."""

    args: list[str]
    stdout: str
    stderr: str
    returncode: int


@dataclass
class BlockDevice:
    """Flattened block device details from lsblk."""

    path: str
    name: str
    kernel_name: str
    type: str
    size_bytes: int
    fstype: Optional[str]
    mountpoints: list[str]
    parent_kernel_name: Optional[str]
    model: Optional[str]
    serial: Optional[str]
    is_removable: bool = False
    children: list["BlockDevice"] = field(default_factory=list)


@dataclass
class MountInfo:
    """Mount information from findmnt."""

    target: str
    source: str
    fstype: str
    options: list[str]


@dataclass
class DockerState:
    """Current Docker storage state."""

    docker_root_dir: str
    storage_driver: Optional[str]
    backing_filesystem: Optional[str]
    supports_d_type: Optional[bool]
    service_running: bool
    docker_info_available: bool
    docker_info_error: Optional[str] = None
    driver_status: dict[str, str] = field(default_factory=dict)
    server_version: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreflightResult:
    """Host-level requirements and collected facts."""

    os_name: str
    distro_id: Optional[str]
    root_mount: MountInfo
    docker_mount: MountInfo
    docker_state: DockerState
    block_devices: list[BlockDevice]


@dataclass
class TargetPlan:
    """Resolved target device/partition and required changes."""

    selection_mode: str
    requested_device: Optional[str]
    resolved_target: str
    parent_disk: str
    classification: str
    needs_partition_creation: bool
    needs_format: bool
    existing_fs_type: Optional[str]
    selection_reason: str
    created_partition_path: Optional[str] = None
    needs_new_gpt: bool = False
    free_region_start_bytes: Optional[int] = None
    free_region_end_bytes: Optional[int] = None


@dataclass
class SetupPlan:
    """Full setup plan for GPU splitting."""

    preflight: PreflightResult
    target: TargetPlan
    backup_dir: str
    daemon_json_path: str
    daemon_json_changed: bool
    daemon_json_merged: dict[str, Any]
    fstab_path: str
    fstab_changed: bool
    destructive_actions: list[str]


@dataclass
class CandidateEvaluation:
    """Read-only evaluation for a discovered storage candidate."""

    path: str
    device_type: str
    classification: str
    allowed: bool
    details: str


@dataclass
class VerificationCheck:
    """One verification row for verify/setup output."""

    name: str
    passed: bool
    actual: str
    expected: str


@dataclass
class VerificationResult:
    """Aggregate verification result."""

    checks: list[VerificationCheck]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)
