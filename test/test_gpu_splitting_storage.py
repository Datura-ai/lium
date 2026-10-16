from __future__ import annotations

from pathlib import Path

import pytest

from lium.cli.mine import storage
from lium.cli.mine.models import BlockDevice, CommandResult, MountInfo
from lium.cli.mine.storage import auto_select_target, resolve_explicit_target


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














