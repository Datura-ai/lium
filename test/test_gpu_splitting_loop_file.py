"""Single-disk hosts: Docker storage in an XFS image file mounted through a loop device (DAH-5046)."""

from __future__ import annotations

import os
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
            loop_file_mountpoint="/", loop_file_size_bytes=180 * GIB, loop_file_reserve_bytes=25 * GIB,
        ),
        backup_dir=str(tmp_path / "docker.lium-backup-20261023120000"),
        daemon_json_path=str(tmp_path / "daemon.json"),
        daemon_json_changed=True,
        daemon_json_merged={"storage-driver": "overlay2"},
        fstab_path=str(tmp_path / "fstab"),
        fstab_changed=True,
        destructive_actions=[],
        drop_in_path=str(tmp_path / "docker.service.d" / "lium-docker-storage.conf"),
        udev_rule_path=str(tmp_path / "rules.d" / "99-lium-docker-storage.rules"),
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
    assert any(action.startswith("switch Docker from the containerd image store to overlay2") for action in plan.destructive_actions)
    assert any(action.startswith("add the udev rule /etc/udev/rules.d/99-lium-docker-storage.rules") for action in plan.destructive_actions)
    assert any("docker.service drop-in" in action for action in plan.destructive_actions)


def test_plan_skips_the_image_file_fallback_when_docker_has_its_own_disk(monkeypatch):
    preflight = _sample_preflight()
    preflight.docker_mount = MountInfo(target="/var/lib/docker", source="/dev/sdc1", fstype="ext4", options=["rw"])
    monkeypatch.setattr(gpu_splitting, "auto_select_target", lambda *a: (_ for _ in ()).throw(NoSafeTargetError("none")))
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: pytest.fail("planned an image file"))

    with pytest.raises(RuntimeError, match=r"its own disk \(/dev/sdc1\); format a spare disk with --device"):
        gpu_splitting._build_setup_plan(preflight, None)


def test_harden_loop_device_only_warns_when_direct_io_is_refused(monkeypatch):
    warnings = []

    def run_command(args, check=True, capture=True):
        if args[:2] == ["losetup", "-j"]:
            return CommandResult(args, "/dev/loop5\n", "", 0)
        if args[:2] == ["losetup", "--direct-io=on"]:
            assert check is False
            return CommandResult(args, "", "losetup: /dev/loop5: set direct io failed: Invalid argument", 1)
        return CommandResult(args, "", "", 0)

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "_write_sysfs", lambda *a: None)
    monkeypatch.setattr(gpu_splitting.console, "warning", warnings.append)

    gpu_splitting._harden_loop_device("/var/lib/lium-docker.xfs")

    assert len(warnings) == 1 and "Direct I/O could not be turned on for /dev/loop5" in warnings[0]


def test_plan_refuses_device_together_with_file(monkeypatch):
    with pytest.raises(RuntimeError, match="pass one of them"):
        gpu_splitting._build_setup_plan(_sample_preflight(), "/dev/sdb", image_file="/pool.xfs")


def test_fstab_entry_and_drop_in_for_the_image_file():
    assert gpu_splitting._fstab_entry("/var/lib/lium-docker.xfs", gpu_splitting.LOOP_FILE_FSTAB_OPTIONS) == (
        "/var/lib/lium-docker.xfs /var/lib/docker xfs loop,pquota,noatime 0 0"
    )
    assert gpu_splitting._docker_drop_in("/var/lib/docker") == "[Unit]\nRequiresMountsFor=/var/lib/docker\n"
    rule = gpu_splitting._loop_device_udev_rule("/var/lib/lium-docker.xfs")
    assert 'ATTR{loop/backing_file}=="/var/lib/lium-docker.xfs"' in rule
    assert 'ATTR{queue/discard_max_bytes}="0"' in rule
    assert 'RUN+="/sbin/losetup --direct-io=on $devnode"' in rule
    assert "sh -c" not in rule


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


class _Commands(list):
    created: list[tuple[str, int]]
    sysfs: dict[str, str]


def _loop_file_host(
    monkeypatch, plan: SetupPlan, fail_at: list[str] | None = None, and_fail: list[str] | None = None, and_fail_from: int = 1
) -> _Commands:
    # and_fail fails from its and_fail_from-th run on: the rsync branch unmounts the original root once in the flow
    commands = _Commands()
    mounted = {"on_target": False}
    docker_root = plan.preflight.docker_state.docker_root_dir

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args == fail_at or (args == and_fail and commands.count(args) >= and_fail_from):
            raise RuntimeError(f"{args[0]} failed")
        if args[:2] == ["losetup", "-j"]:
            return CommandResult(args=args, stdout="/dev/loop5\n", stderr="", returncode=0)
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
    created: list[tuple[str, int]] = []

    def create_image_file(path, size_bytes):
        created.append((path, size_bytes))
        Path(path).write_bytes(b"")

    monkeypatch.setattr(gpu_splitting, "_create_image_file", create_image_file)
    sysfs: dict[str, str] = {}
    monkeypatch.setattr(gpu_splitting, "_write_sysfs", lambda path, value: sysfs.__setitem__(path, value))
    commands.created = created
    commands.sysfs = sysfs
    Path(docker_root).mkdir()
    (Path(docker_root) / "image-layers").write_text("x")
    return commands


@pytest.mark.skipif(not hasattr(os, "posix_fallocate"), reason="posix_fallocate is Linux-only")
def test_create_image_file_is_exclusive_and_private(tmp_path):
    image = tmp_path / "lium-docker.xfs"

    gpu_splitting._create_image_file(str(image), 4096)

    assert image.stat().st_mode & 0o777 == 0o600
    assert image.stat().st_size == 4096


@pytest.mark.parametrize("planted", ["file", "symlink"])
def test_create_image_file_refuses_a_planted_path(tmp_path, planted):
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    image = tmp_path / "lium-docker.xfs"
    if planted == "symlink":
        image.symlink_to(victim)
    else:
        image.write_text("mine")

    with pytest.raises(RuntimeError, match="appeared after the plan was made"):
        gpu_splitting._create_image_file(str(image), 4096)

    assert victim.read_text() == "keep me"


def _directory_chain(monkeypatch, entries: dict[str, tuple[int, int]]):
    # entries: directory -> (uid, st_mode); the image file's own path never exists
    monkeypatch.setattr(gpu_splitting.os, "lstat", lambda path: SimpleNamespace(st_uid=entries[path][0], st_mode=entries[path][1]))
    monkeypatch.setattr(gpu_splitting.os.path, "lexists", lambda path: False)


ROOT_ONLY = (0, 0o40755)


@pytest.mark.parametrize(
    "entries, match",
    [
        ({"/srv": ROOT_ONLY, "/srv/alice": (1000, 0o40755), "/srv/alice/pool": ROOT_ONLY}, "/srv/alice must be .*uid 1000"),
        ({"/srv": ROOT_ONLY, "/srv/disk": (0, 0o120777), "/srv/disk/pool": ROOT_ONLY}, "/srv/disk must be .*symlink"),
        ({"/srv": ROOT_ONLY, "/srv/shared": (0, 0o41777), "/srv/shared/pool": ROOT_ONLY}, "/srv/shared must be .*mode 777"),
        ({"/srv": ROOT_ONLY, "/srv/group": (0, 0o40775)}, "/srv/group must be .*mode 775"),
    ],
)
def test_image_file_parent_refuses_an_unsafe_ancestor(monkeypatch, entries, match):
    _directory_chain(monkeypatch, entries)
    parent = list(entries)[-1]

    with pytest.raises(RuntimeError, match=match):
        gpu_splitting._check_image_file_parent(f"{parent}/lium-docker.xfs")


def test_image_file_parent_chain_root_only_passes(monkeypatch):
    _directory_chain(monkeypatch, {"/var": ROOT_ONLY, "/var/lib": ROOT_ONLY})

    gpu_splitting._check_image_file_parent("/var/lib/lium-docker.xfs")


def test_image_file_parent_refuses_an_existing_target(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_splitting.os, "lstat", lambda path: SimpleNamespace(st_uid=0, st_mode=0o40755))
    (tmp_path / "lium-docker.xfs").write_text("planted")

    with pytest.raises(RuntimeError, match="already exists"):
        gpu_splitting._check_image_file_parent(str(tmp_path / "lium-docker.xfs"))


def test_image_file_parent_root_only_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(gpu_splitting.os, "lstat", lambda path: SimpleNamespace(st_uid=0, st_mode=0o40755))
    monkeypatch.setattr(gpu_splitting.os.path, "lexists", lambda path: False)

    gpu_splitting._check_image_file_parent(str(tmp_path / "lium-docker.xfs"))


def test_pre_stop_checks_refuse_docker_data_larger_than_the_image_file(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return CommandResult(args, "", "", 0)
        if args[0] == "du":
            return CommandResult(args, f"{181 * GIB}\t{plan.preflight.docker_state.docker_root_dir}\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "_check_image_file_parent", lambda *_: None)
    with pytest.raises(RuntimeError, match="181.0 GiB, more than the 180.0 GiB image file"):
        gpu_splitting._pre_stop_checks(plan)
    assert ["systemctl", "stop", "docker"] not in commands


def test_execute_setup_image_file_moves_data_aside_preallocates_and_wires_docker_to_the_mount(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    image = plan.target.resolved_target

    gpu_splitting._execute_setup(plan)

    assert commands[:2] == [["systemctl", "stop", "docker"], ["systemctl", "stop", "docker.socket"]]
    assert ["mv", docker_root, plan.backup_dir] in commands
    assert commands.created == [(image, 180 * GIB)]
    assert ["mkfs.xfs", "-f", "-K", image] in commands
    assert ["systemctl", "daemon-reload"] in commands
    assert ["udevadm", "control", "--reload-rules"] in commands
    assert commands.index(["mount", docker_root]) < commands.index(["udevadm", "trigger", "--action=change", "/dev/loop5"])
    assert commands.sysfs == {"/sys/block/loop5/queue/discard_max_bytes": "0"}
    assert ["losetup", "--direct-io=on", "/dev/loop5"] in commands
    assert ["mount", docker_root] in commands
    assert ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"] in commands
    assert commands[-1] == ["systemctl", "start", "docker"]
    assert not any(args[0] in {"blkid", "parted", "rsync"} and args[-1] == f"{plan.backup_dir}/" for args in commands)
    assert Path(plan.fstab_path).read_text() == f"{image} /var/lib/docker xfs loop,pquota,noatime 0 0\n"
    assert Path(plan.drop_in_path).read_text() == f"[Unit]\nRequiresMountsFor={docker_root}\n"
    assert f'ATTR{{loop/backing_file}}=="{image}"' in Path(plan.udev_rule_path).read_text()
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
    assert not Path(plan.udev_rule_path).exists()
    assert Path(plan.fstab_path).read_text() == "/dev/sda1 / ext4 defaults 0 1\n"
    assert commands.count(["systemctl", "daemon-reload"]) == 2
    assert commands.count(["udevadm", "control", "--reload-rules"]) == 2
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_create_image_file_removes_the_file_when_preallocation_fails(monkeypatch, tmp_path):
    def enospc(fd, offset, size):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "posix_fallocate", enospc, raising=False)
    image = tmp_path / "lium-docker.xfs"

    with pytest.raises(OSError):
        gpu_splitting._create_image_file(str(image), 4096)

    assert not image.exists()


def test_execute_setup_image_file_rollback_before_cutover_moves_data_back(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    docker_root = plan.preflight.docker_state.docker_root_dir
    commands = _loop_file_host(monkeypatch, plan, fail_at=["mkfs.xfs", "-f", "-K", plan.target.resolved_target])

    with pytest.raises(RuntimeError, match="mkfs.xfs failed"):
        gpu_splitting._execute_setup(plan)

    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert not Path(plan.backup_dir).exists()
    assert not any(args[0] == "rmdir" for args in commands)
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_execute_setup_image_file_rollback_keeps_the_image_and_docker_down_when_unmount_fails(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    docker_root = plan.preflight.docker_state.docker_root_dir
    restore = ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]
    commands = _loop_file_host(monkeypatch, plan, fail_at=restore, and_fail=["umount", docker_root])

    with pytest.raises(RuntimeError, match="rsync failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert "still mounted" in str(exc_info.value)
    assert f"The XFS image file {plan.target.resolved_target} was kept" in str(exc_info.value)
    assert Path(plan.target.resolved_target).exists()
    assert Path(plan.backup_dir).exists()
    assert ["systemctl", "start", "docker"] not in commands
    assert not any(args[0] in {"rmdir", "mv"} and args[-1] == docker_root for args in commands)


@pytest.mark.parametrize("also_fails, expected_error", [("umount", "still mounted"), ("remount", "remount original Docker root")])
def test_execute_setup_rsync_branch_rollback_keeps_docker_down_until_the_original_root_is_back(
    monkeypatch, tmp_path, also_fails, expected_error
):
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    restore = ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]
    remount = ["mount", "-t", "ext4", "-o", "rw", "/dev/sdc1", docker_root]
    and_fail = ["umount", docker_root] if also_fails == "umount" else remount
    commands = _loop_file_host(monkeypatch, plan, fail_at=restore, and_fail=and_fail, and_fail_from=2 if also_fails == "umount" else 1)

    with pytest.raises(RuntimeError, match="rsync failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert expected_error in str(exc_info.value)
    assert "start docker: skipped" in str(exc_info.value)
    assert ["systemctl", "start", "docker"] not in commands
    assert ["mv", plan.backup_dir, docker_root] not in commands


@pytest.mark.parametrize("remount_fails", [False, True])
def test_execute_setup_rsync_branch_remounts_the_original_root_when_the_image_mount_fails(monkeypatch, tmp_path, remount_fails):
    # the original partition is unmounted before the image mount; a failed image mount must not leave it off
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    remount = ["mount", "-t", "ext4", "-o", "rw", "/dev/sdc1", docker_root]
    commands = _loop_file_host(monkeypatch, plan, fail_at=["mount", docker_root], and_fail=remount if remount_fails else None)

    with pytest.raises(RuntimeError, match="mount failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert commands.index(["umount", docker_root]) < commands.index(remount)
    if remount_fails:
        assert "start docker: skipped" in str(exc_info.value)
        assert ["systemctl", "start", "docker"] not in commands
    else:
        assert commands.index(remount) < commands.index(["systemctl", "start", "docker"])
    assert not Path(plan.target.resolved_target).exists()


def test_execute_setup_rsync_branch_rollback_restarts_docker_on_the_original_root(monkeypatch, tmp_path):
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    restore = ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]
    commands = _loop_file_host(monkeypatch, plan, fail_at=restore)

    with pytest.raises(RuntimeError, match="The XFS image file was removed"):
        gpu_splitting._execute_setup(plan)

    assert commands.index(["mount", "-t", "ext4", "-o", "rw", "/dev/sdc1", docker_root]) < commands.index(["systemctl", "start", "docker"])
    assert not Path(plan.target.resolved_target).exists()


def test_pre_stop_checks_refuse_containers_left_in_the_containerd_store(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    plan.preflight.docker_state.storage_driver = "overlayfs"

    def run_command(args, check=True, capture=True):
        if args == ["docker", "ps", "-q"]:
            return CommandResult(args, "", "", 0)
        if args == ["docker", "ps", "-aq"]:
            return CommandResult(args, "abc\ndef\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    with pytest.raises(RuntimeError, match="2 containers exist in Docker's containerd image store"):
        gpu_splitting._pre_stop_checks(plan)


@pytest.mark.parametrize("free_gib, passes", [(300, True), (250, False)])
def test_pre_stop_checks_count_the_image_file_beside_the_backup_copy(monkeypatch, tmp_path, free_gib, passes):
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)

    def run_command(args, check=True, capture=True):
        if args[:2] == ["docker", "ps"]:
            return CommandResult(args, "", "", 0)
        if args[0] == "du":
            return CommandResult(args, f"{100 * GIB}\t{plan.preflight.docker_state.docker_root_dir}\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "_check_image_file_parent", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_same_filesystem", lambda *_: True)
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(free=free_gib * GIB))

    if passes:
        gpu_splitting._pre_stop_checks(plan)
    else:
        with pytest.raises(RuntimeError, match="and the image file needs 291.0 GiB"):
            gpu_splitting._pre_stop_checks(plan)


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
