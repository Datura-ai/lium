from __future__ import annotations

from pathlib import Path

import pytest

from lium.cli.mine import storage
from lium.cli.mine.docker_config import inspect_docker_state, load_daemon_json, merge_overlay2
from lium.cli.mine.models import BlockDevice, CommandResult, MountInfo
from lium.cli.mine.storage import auto_select_target, load_mount_info, resolve_explicit_target


def _runner_factory(responses: dict[tuple[str, ...], CommandResult]):
    def runner(args: list[str], check: bool = True, capture: bool = True) -> CommandResult:
        key = tuple(args)
        if key not in responses:
            raise AssertionError(f"Unexpected command: {args}")
        return responses[key]

    return runner


def _root_mount() -> MountInfo:
    return MountInfo(target="/", source="/dev/nvme0n1p2", fstype="ext4", options=["rw"])


def _docker_mount() -> MountInfo:
    return MountInfo(target="/", source="/dev/nvme0n1p2", fstype="ext4", options=["rw"])


def test_resolve_explicit_blank_disk(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, strict=True: Path(str(self)))
    devices = [
        BlockDevice(
            path="/dev/nvme1n1",
            name="nvme1n1",
            kernel_name="nvme1n1",
            type="disk",
            size_bytes=1_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name=None,
            model=None,
            serial=None,
            children=[],
        )
    ]
    runner = _runner_factory({})

    plan = resolve_explicit_target(runner, devices, _root_mount(), _docker_mount(), "/dev/nvme1n1")

    assert plan.classification == "blank_whole_disk"
    assert plan.needs_partition_creation is True
    assert plan.needs_format is True
    assert plan.parent_disk == "/dev/nvme1n1"


def test_resolve_explicit_ext4_partition_rejected(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, strict=True: Path(str(self)))
    devices = [
        BlockDevice(
            path="/dev/nvme1n1p1",
            name="nvme1n1p1",
            kernel_name="nvme1n1p1",
            type="part",
            size_bytes=1_000_000,
            fstype="ext4",
            mountpoints=[],
            parent_kernel_name="nvme1n1",
            model=None,
            serial=None,
            children=[],
        )
    ]
    runner = _runner_factory({})

    with pytest.raises(RuntimeError, match="partition_existing_fs_reject"):
        resolve_explicit_target(runner, devices, _root_mount(), _docker_mount(), "/dev/nvme1n1p1")


def test_resolve_explicit_xfs_partition_with_data_rejected(monkeypatch):
    monkeypatch.setattr(Path, "resolve", lambda self, strict=True: Path(str(self)))
    monkeypatch.setattr(storage, "_partition_appears_to_contain_live_data", lambda runner, path: (True, "filesystem not empty"))
    devices = [
        BlockDevice(
            path="/dev/nvme1n1p1",
            name="nvme1n1p1",
            kernel_name="nvme1n1p1",
            type="part",
            size_bytes=1_000_000,
            fstype="xfs",
            mountpoints=[],
            parent_kernel_name="nvme1n1",
            model=None,
            serial=None,
            children=[],
        )
    ]
    runner = _runner_factory(
        {
            ("xfs_info", "/dev/nvme1n1p1"): CommandResult(
                args=["xfs_info", "/dev/nvme1n1p1"],
                stdout="naming   =version 2              bsize=4096   ascii-ci=0, ftype=1\n",
                stderr="",
                returncode=0,
            )
        }
    )

    with pytest.raises(RuntimeError, match="unsupported"):
        resolve_explicit_target(runner, devices, _root_mount(), _docker_mount(), "/dev/nvme1n1p1")


def test_auto_select_prefers_blank_disk_over_empty_partition():
    devices = [
        BlockDevice(
            path="/dev/nvme2n1p1",
            name="nvme2n1p1",
            kernel_name="nvme2n1p1",
            type="part",
            size_bytes=1_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name="nvme2n1",
            model=None,
            serial=None,
            children=[],
        ),
        BlockDevice(
            path="/dev/nvme1n1",
            name="nvme1n1",
            kernel_name="nvme1n1",
            type="disk",
            size_bytes=2_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name=None,
            model=None,
            serial=None,
            children=[],
        ),
    ]

    plan = auto_select_target(_runner_factory({}), devices, _root_mount(), _docker_mount())

    assert plan.resolved_target == "/dev/nvme1n1"
    assert plan.classification == "blank_whole_disk"


def test_auto_select_rejects_multiple_blank_disks():
    devices = [
        BlockDevice(
            path="/dev/nvme1n1",
            name="nvme1n1",
            kernel_name="nvme1n1",
            type="disk",
            size_bytes=2_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name=None,
            model=None,
            serial=None,
            children=[],
        ),
        BlockDevice(
            path="/dev/nvme2n1",
            name="nvme2n1",
            kernel_name="nvme2n1",
            type="disk",
            size_bytes=2_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name=None,
            model=None,
            serial=None,
            children=[],
        ),
    ]

    with pytest.raises(RuntimeError, match="Multiple equally safe storage targets"):
        auto_select_target(_runner_factory({}), devices, _root_mount(), _docker_mount())


def test_auto_select_allows_single_removable_only_when_no_non_removable():
    devices = [
        BlockDevice(
            path="/dev/sdb",
            name="sdb",
            kernel_name="sdb",
            type="disk",
            size_bytes=2_000_000,
            fstype=None,
            mountpoints=[],
            parent_kernel_name=None,
            model=None,
            serial=None,
            is_removable=True,
            children=[],
        )
    ]

    plan = auto_select_target(_runner_factory({}), devices, _root_mount(), _docker_mount())

    assert plan.resolved_target == "/dev/sdb"


def test_merge_overlay2_preserves_other_keys():
    merged, changed = merge_overlay2({"debug": True, "storage-driver": "aufs"})

    assert changed is True
    assert merged == {"debug": True, "storage-driver": "overlay2"}


def test_load_mount_info_uses_findmnt_target_semantics():
    runner = _runner_factory(
        {
            ("findmnt", "--json", "--target", "/var/lib/docker"): CommandResult(
                args=["findmnt", "--json", "--target", "/var/lib/docker"],
                stdout='{"filesystems":[{"target":"/","source":"/dev/sda1","fstype":"ext4","options":"rw,noatime"}]}',
                stderr="",
                returncode=0,
            )
        }
    )

    mount_info = load_mount_info(runner, "/var/lib/docker")

    assert mount_info.target == "/"
    assert mount_info.source == "/dev/sda1"
    assert mount_info.fstype == "ext4"
    assert "rw" in mount_info.options


def test_inspect_docker_state_captures_error_details():
    runner = _runner_factory(
        {
            ("docker", "info", "--format", "{{json .}}"): CommandResult(
                args=["docker", "info", "--format", "{{json .}}"],
                stdout="",
                stderr="permission denied while trying to connect to the docker API at unix:///var/run/docker.sock",
                returncode=1,
            )
        }
    )

    state = inspect_docker_state(runner)

    assert state.docker_info_available is False
    assert state.docker_info_error is not None
    assert "permission denied" in state.docker_info_error


def test_load_daemon_json_invalid(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text("{not-json}")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        load_daemon_json(str(path))
