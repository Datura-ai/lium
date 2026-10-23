"""Single-disk hosts: Docker storage in an XFS image file mounted through a loop device (DAH-5046)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gpu_splitting_cli import (
    _patch_execute_setup_dependencies,
    _sample_preflight,
)

from lium.cli.mine import gpu_splitting
from lium.cli.mine.models import (
    CommandResult,
    MountedFilesystem,
    MountInfo,
    SetupPlan,
    TargetPlan,
)
from lium.cli.mine.storage import NoSafeTargetError

GIB = 1024**3


def _loop_file_plan(tmp_path, docker_mount: MountInfo | None = None) -> SetupPlan:
    preflight = _sample_preflight()
    preflight.docker_state.docker_root_dir = str(tmp_path / "docker")
    if docker_mount is not None:
        preflight.docker_mount = docker_mount
    return SetupPlan(
        preflight=preflight,
        target=TargetPlan(
            selection_mode="auto", requested_device=None, resolved_target=str(tmp_path / "lium-docker.xfs"),
            parent_disk="/dev/nvme0n1p2", classification="loop_file", needs_partition_creation=False, needs_format=True,
            existing_fs_type=None, selection_reason="No spare disk or partition; XFS image file on /",
            loop_file_filesystem="/", loop_file_size_bytes=180 * GIB, loop_file_reserve_bytes=25 * GIB,
        ),
        backup_dir=str(tmp_path / "docker.lium-backup-20261023120000"),
        daemon_json_path=str(tmp_path / "daemon.json"),
        daemon_json_changed=True,
        daemon_json_merged={"storage-driver": "overlay2"},
        fstab_path=str(tmp_path / "fstab"),
        fstab_changed=True,
        destructive_actions=[],
        drop_in_path=str(tmp_path / "docker.service.d" / "lium-docker-storage.conf"),
    )


def _mounts(plan: SetupPlan, mounted: dict):
    # the Docker root is a plain directory on / until `mount <root>` runs, then an XFS loop mount with pquota
    def load_mount_info(runner, target):
        if mounted["on_target"]:
            return MountInfo(target=target, source="/dev/loop0", fstype="xfs", options=["rw", "noatime", "prjquota"])
        return plan.preflight.docker_mount

    return load_mount_info


# --- plan ------------------------------------------------------------------------------------------------------


def test_plan_falls_back_to_an_image_file_when_no_disk_is_safe(monkeypatch, tmp_path):
    preflight = _sample_preflight()
    preflight.docker_state.storage_driver = "overlayfs"   # Docker 29 with the containerd image store
    monkeypatch.setattr(gpu_splitting, "auto_select_target", lambda *a: (_ for _ in ()).throw(NoSafeTargetError("none")))
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [
        MountedFilesystem(target="/", source="/dev/nvme0n1p2", fstype="ext4", size_bytes=250 * GIB, avail_bytes=200 * GIB, local=True)
    ])
    monkeypatch.setattr(gpu_splitting, "load_daemon_json", lambda *a: {})
    monkeypatch.setattr(gpu_splitting, "_fstab_would_change", lambda *a: True)

    plan = gpu_splitting._build_setup_plan(preflight, None)

    assert plan.target.classification == "loop_file" and plan.target.resolved_target == "/var/lib/lium-docker.xfs"
    assert plan.target.loop_file_size_bytes == 175 * GIB and plan.target.loop_file_reserve_bytes == 25 * GIB
    assert plan.daemon_json_merged == {"storage-driver": "overlay2", "features": {"containerd-snapshotter": False}}
    assert "create the XFS image file /var/lib/lium-docker.xfs: 175 GiB on /, 25 GiB stay free for the system" in plan.destructive_actions
    assert "move Docker data aside (same filesystem, no copy)" in plan.destructive_actions
    assert "switch Docker from the containerd image store to overlay2 (images are pulled again)" in plan.destructive_actions
    assert any("docker.service drop-in" in action for action in plan.destructive_actions)


def test_plan_refuses_device_together_with_file(monkeypatch):
    with pytest.raises(RuntimeError, match="pass one of them"):
        gpu_splitting._build_setup_plan(_sample_preflight(), "/dev/sdb", image_file="/pool.xfs")


def test_fstab_entry_and_drop_in_for_the_image_file():
    assert gpu_splitting._fstab_entry("/var/lib/lium-docker.xfs", gpu_splitting.LOOP_FILE_FSTAB_OPTIONS) == (
        "/var/lib/lium-docker.xfs /var/lib/docker xfs loop,pquota,noatime 0 0"
    )
    drop_in = gpu_splitting._docker_drop_in("/var/lib/docker", "/var/lib/lium-docker.xfs")
    assert "RequiresMountsFor=/var/lib/docker\n" in drop_in
    assert 'ExecStartPre=-/bin/sh -c "losetup --direct-io=on $(losetup -j /var/lib/lium-docker.xfs -n -O NAME)"' in drop_in


def test_confirm_yes_is_enough_for_an_image_file(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_splitting.console, "print", lambda *a, **k: None)
    gpu_splitting._confirm_plan(_loop_file_plan(tmp_path), yes=True)


def test_confirm_asks_for_the_image_file_path(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    monkeypatch.setattr(gpu_splitting.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(gpu_splitting.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    prompts = []
    monkeypatch.setattr(gpu_splitting.Prompt, "ask", lambda prompt, **k: prompts.append(prompt) or plan.target.resolved_target)

    gpu_splitting._confirm_plan(plan, yes=False)

    assert prompts == [f"Type the image file path to create it ({plan.target.resolved_target})"]


# --- execute -----------------------------------------------------------------------------------------------------


def _loop_file_host(monkeypatch, plan: SetupPlan, fail_at: list[str] | None = None):
    commands: list[list[str]] = []
    mounted = {"on_target": False}
    docker_root = plan.preflight.docker_state.docker_root_dir

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args == (fail_at or []):
            raise RuntimeError(f"{args[0]} failed")
        if args[0] == "fallocate":
            Path(args[-1]).write_bytes(b"")
        if args[0] == "mv" and args[1] == docker_root:
            Path(args[1]).rename(args[2])
        if args[0] == "mv" and args[2] == docker_root:
            Path(args[1]).rename(args[2])
        if args[0] == "rmdir":
            Path(args[1]).rmdir()
        if args == ["mount", docker_root]:
            mounted["on_target"] = True
        if args == ["umount", docker_root]:
            mounted["on_target"] = False
        return CommandResult(args=args, stdout="", stderr="", returncode=0)

    _patch_execute_setup_dependencies(monkeypatch, run_command, _mounts(plan, mounted))
    monkeypatch.setattr(gpu_splitting, "_pre_stop_checks", lambda *_: None)
    monkeypatch.setattr(gpu_splitting.os, "chmod", lambda *_: None)
    Path(docker_root).mkdir()
    (Path(docker_root) / "image-layers").write_text("x")
    return commands


def test_execute_setup_image_file_moves_data_aside_preallocates_and_wires_docker_to_the_mount(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    image = plan.target.resolved_target

    gpu_splitting._execute_setup(plan)

    assert commands[:2] == [["systemctl", "stop", "docker"], ["systemctl", "stop", "docker.socket"]]
    assert ["mv", docker_root, plan.backup_dir] in commands
    assert ["fallocate", "-l", str(180 * GIB), image] in commands
    assert ["mkfs.xfs", "-f", "-K", image] in commands
    assert ["systemctl", "daemon-reload"] in commands
    assert ["mount", docker_root] in commands
    assert ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"] in commands
    assert commands[-1] == ["systemctl", "start", "docker"]
    assert not any(args[0] in {"blkid", "parted", "rsync"} and args[-1] == f"{plan.backup_dir}/" for args in commands)
    assert Path(plan.fstab_path).read_text() == f"{image} /var/lib/docker xfs loop,pquota,noatime 0 0\n"
    assert f"RequiresMountsFor={docker_root}" in Path(plan.drop_in_path).read_text()
    assert not Path(plan.backup_dir).exists()


def test_execute_setup_image_file_copies_when_the_docker_root_is_its_own_mount(monkeypatch, tmp_path):
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir

    gpu_splitting._execute_setup(plan)

    assert ["rsync", "-aHAX", "--numeric-ids", f"{docker_root}/", f"{plan.backup_dir}/"] in commands
    assert not any(args[0] == "mv" for args in commands)


def test_execute_setup_image_file_rollback_moves_data_back_and_removes_the_file(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    docker_root = plan.preflight.docker_state.docker_root_dir
    Path(plan.fstab_path).write_text("/dev/sda1 / ext4 defaults 0 1\n")
    commands = _loop_file_host(monkeypatch, plan, fail_at=["mount", docker_root])

    with pytest.raises(RuntimeError, match="mount failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert "The XFS image file was removed." in str(exc_info.value)
    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert not Path(plan.backup_dir).exists()
    assert not Path(plan.target.resolved_target).exists()
    assert not Path(plan.drop_in_path).exists()
    assert Path(plan.fstab_path).read_text() == "/dev/sda1 / ext4 defaults 0 1\n"
    assert commands.count(["systemctl", "daemon-reload"]) == 2
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_execute_setup_image_file_rollback_after_restore_failure_unmounts_first(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    docker_root = plan.preflight.docker_state.docker_root_dir
    restore = ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]
    commands = _loop_file_host(monkeypatch, plan, fail_at=restore)

    with pytest.raises(RuntimeError, match="rsync failed"):
        gpu_splitting._execute_setup(plan)

    assert commands.index(["umount", docker_root]) < commands.index(["rmdir", docker_root])
    assert ["mv", plan.backup_dir, docker_root] in commands
    assert (Path(docker_root) / "image-layers").exists()
