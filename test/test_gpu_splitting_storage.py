from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from lium.cli.mine import gpu_splitting, storage
from lium.cli.mine.docker_config import merge_overlay2
from lium.cli.mine.models import (
    BlockDevice,
    CommandResult,
    DockerState,
    MountedFilesystem,
    MountInfo,
    PreflightResult,
    SetupPlan,
    TargetPlan,
    VerificationCheck,
    VerificationResult,
)
from lium.cli.mine.storage import (
    NoSafeTargetError,
    _source_disk_is_local,
    auto_select_target,
    load_mounted_filesystems,
    plan_loop_file_target,
    resolve_explicit_target,
)


GIB = 1024**3


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


# A failed _execute_setup must leave the host as it found it: Docker restarted, daemon.json/fstab and the original
# mount restored, rollback errors reported (moved from test_gpu_splitting_cli.py, DAH-4027 review).
@contextmanager
def _no_spinner(*args, **kwargs):
    yield


def _sample_preflight() -> PreflightResult:
    return PreflightResult(
        os_name="Linux",
        distro_id="ubuntu",
        root_mount=MountInfo(target="/", source="/dev/nvme0n1p2", fstype="ext4", options=["rw"]),
        docker_mount=MountInfo(target="/", source="/dev/nvme0n1p2", fstype="ext4", options=["rw"]),
        docker_state=DockerState(
            docker_root_dir="/var/lib/docker",
            storage_driver="overlay2",
            backing_filesystem="xfs",
            supports_d_type=True,
            service_running=True,
            docker_info_available=True,
            driver_status={"Backing Filesystem": "xfs", "Supports d_type": "true"},
        ),
        block_devices=[],
    )


def _sample_plan() -> SetupPlan:
    return SetupPlan(
        preflight=_sample_preflight(),
        target=TargetPlan(
            selection_mode="auto",
            requested_device=None,
            resolved_target="/dev/nvme1n1",
            parent_disk="/dev/nvme1n1",
            classification="blank_whole_disk",
            needs_partition_creation=True,
            needs_format=True,
            existing_fs_type=None,
            selection_reason="Auto-selected blank_whole_disk",
            needs_new_gpt=True,
        ),
        backup_dir="/var/lib/docker.lium-backup-20260303120000",
        daemon_json_path="/etc/docker/daemon.json",
        daemon_json_changed=True,
        daemon_json_merged={"storage-driver": "overlay2"},
        fstab_path="/etc/fstab",
        fstab_changed=True,
        destructive_actions=[
            "create GPT label on /dev/nvme1n1",
            "create partition on /dev/nvme1n1",
            "format target as XFS: /dev/nvme1n1",
        ],
    )


def _execution_plan(tmp_path, docker_mount: MountInfo | None = None) -> SetupPlan:
    preflight = _sample_preflight()
    if docker_mount is not None:
        preflight.docker_mount = docker_mount

    return SetupPlan(
        preflight=preflight,
        target=TargetPlan(
            selection_mode="explicit",
            requested_device="/dev/vdb1",
            resolved_target="/dev/vdb1",
            parent_disk="/dev/vdb",
            classification="empty_partition",
            needs_partition_creation=False,
            needs_format=False,
            existing_fs_type="xfs",
            selection_reason="Selected explicit empty XFS partition",
            needs_new_gpt=False,
        ),
        backup_dir=str(tmp_path / "docker-backup"),
        daemon_json_path=str(tmp_path / "daemon.json"),
        daemon_json_changed=True,
        daemon_json_merged={"storage-driver": "overlay2"},
        fstab_path=str(tmp_path / "fstab"),
        fstab_changed=True,
        destructive_actions=[],
    )


def _patch_execute_setup_dependencies(monkeypatch, run_command, load_mount_info, verification=None):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_pre_stop_checks", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_partition_paths_for_disk", lambda *_: set())
    monkeypatch.setattr(gpu_splitting, "_ensure_xfs_compliant", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_mount_validation", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_get_uuid", lambda *_: "uuid-test")
    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "load_mount_info", load_mount_info)
    monkeypatch.setattr(
        gpu_splitting,
        "build_verification_result",
        verification
        or (
            lambda runner, docker_root_dir="/var/lib/docker": VerificationResult(
                checks=[
                    VerificationCheck(
                        name="Mount options include project quota",
                        passed=True,
                        actual="rw,pquota",
                        expected="contains pquota or prjquota",
                    )
                ]
            )
        ),
    )


def test_execute_setup_restarts_docker_when_failure_happens_after_stop(monkeypatch, tmp_path):
    plan = _execution_plan(tmp_path)
    commands: list[list[str]] = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:3] == ["systemctl", "stop", "docker"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args == ["systemctl", "stop", "docker.socket"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:2] == ["rsync", "-aHAX"]:
            raise RuntimeError("backup failed")
        if args == ["systemctl", "start", "docker"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {args}")

    _patch_execute_setup_dependencies(
        monkeypatch,
        run_command,
        lambda runner, target: plan.preflight.docker_mount,
    )

    with pytest.raises(RuntimeError, match="GPU splitting setup failed: backup failed"):
        gpu_splitting._execute_setup(plan)

    assert ["systemctl", "start", "docker"] in commands


def test_execute_setup_restores_config_files_after_config_stage_failure(monkeypatch, tmp_path):
    plan = _execution_plan(tmp_path)
    daemon_path = Path(plan.daemon_json_path)
    daemon_path.write_text('{"debug": true}\n')
    fstab_path = Path(plan.fstab_path)
    fstab_path.write_text("/dev/sda1 / ext4 defaults 0 1\n")
    commands: list[list[str]] = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["systemctl", "stop"] or args == ["systemctl", "start", "docker"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:2] == ["rsync", "-aHAX"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:3] == ["blkid", "-s", "UUID"]:
            return gpu_splitting.CommandResult(args=args, stdout="uuid-test\n", stderr="", returncode=0)
        if args == ["mount", "/var/lib/docker"]:
            raise RuntimeError("mount failed")
        raise AssertionError(f"Unexpected command: {args}")

    _patch_execute_setup_dependencies(
        monkeypatch,
        run_command,
        lambda runner, target: plan.preflight.docker_mount,
    )

    with pytest.raises(RuntimeError, match="mount failed"):
        gpu_splitting._execute_setup(plan)

    assert daemon_path.read_text() == '{"debug": true}\n'
    assert fstab_path.read_text() == "/dev/sda1 / ext4 defaults 0 1\n"
    assert ["systemctl", "start", "docker"] in commands


def test_execute_setup_restores_original_mount_after_cutover_failure(monkeypatch, tmp_path):
    docker_mount = MountInfo(
        target="/var/lib/docker",
        source="/dev/mapper/docker-old",
        fstype="xfs",
        options=["rw", "pquota"],
    )
    plan = _execution_plan(tmp_path, docker_mount=docker_mount)
    commands: list[list[str]] = []
    mount_state = {"phase": "original"}

    Path(plan.daemon_json_path).write_text('{"storage-driver": "aufs"}\n')
    Path(plan.fstab_path).write_text("UUID=old /var/lib/docker xfs defaults,pquota 0 0\n")

    def load_mount_info(runner, target):
        if mount_state["phase"] == "original":
            return docker_mount
        return MountInfo(
            target="/var/lib/docker",
            source="/dev/vdb1",
            fstype="xfs",
            options=["rw", "pquota"],
        )

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["systemctl", "stop"] or args == ["systemctl", "start", "docker"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:2] == ["rsync", "-aHAX"]:
            if args[-1] == f"{plan.preflight.docker_state.docker_root_dir}/":
                raise RuntimeError("restore failed")
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:3] == ["blkid", "-s", "UUID"]:
            return gpu_splitting.CommandResult(args=args, stdout="uuid-test\n", stderr="", returncode=0)
        if args == ["umount", "/var/lib/docker"]:
            mount_state["phase"] = "root"
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args == ["mount", "/var/lib/docker"]:
            mount_state["phase"] = "target"
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args == ["mount", "-t", "xfs", "-o", "rw,pquota", "/dev/mapper/docker-old", "/var/lib/docker"]:
            mount_state["phase"] = "original"
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {args}")

    _patch_execute_setup_dependencies(monkeypatch, run_command, load_mount_info)

    with pytest.raises(RuntimeError, match="restore failed"):
        gpu_splitting._execute_setup(plan)

    assert commands.count(["umount", "/var/lib/docker"]) == 2
    assert ["mount", "-t", "xfs", "-o", "rw,pquota", "/dev/mapper/docker-old", "/var/lib/docker"] in commands
    assert ["systemctl", "start", "docker"] in commands


def test_execute_setup_reports_rollback_failures(monkeypatch, tmp_path):
    plan = _execution_plan(tmp_path)
    commands: list[list[str]] = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["systemctl", "stop"]:
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:2] == ["rsync", "-aHAX"]:
            raise RuntimeError("backup failed")
        if args == ["systemctl", "start", "docker"]:
            raise RuntimeError("start failed")
        raise AssertionError(f"Unexpected command: {args}")

    _patch_execute_setup_dependencies(
        monkeypatch,
        run_command,
        lambda runner, target: plan.preflight.docker_mount,
    )

    with pytest.raises(RuntimeError) as exc_info:
        gpu_splitting._execute_setup(plan)

    message = str(exc_info.value)
    assert "GPU splitting setup failed: backup failed" in message
    assert "Rollback encountered additional errors:" in message
    assert "- start docker: start failed" in message
    assert f"Backup remains at {plan.backup_dir}" in message


def _lsblk(blockdevices):
    import json

    def runner(args, check=True, capture=True):
        if args[0] == "lsblk":
            return CommandResult(args, json.dumps({"blockdevices": blockdevices}), "", 0)
        if args[0] == "parted":  # one large free region: what made a system disk look offerable before
            return CommandResult(args, "BYT;\n1:1048576B:2000000000000B:1999998951424B:free;\n", "", 0)
        raise AssertionError(f"Unexpected command: {args}")

    return runner


def _dev(name, type_, children=(), mount=None, pkname=None, path=None):
    return {"name": name, "kname": name, "path": path or f"/dev/{name}", "type": type_, "size": 2 * 1024**4,
            "mountpoints": [mount] if mount else [None], "pkname": pkname, "children": list(children)}


def _verdicts(runner, root_source):
    devices = storage.load_block_devices(runner)
    root = MountInfo(target="/", source=root_source, fstype="ext4", options=["rw"])
    return {c.path: c.classification for c in storage.evaluate_candidates(runner, devices, root, root)}


def test_disk_under_an_lvm_root_is_protected():
    lv = _dev("dm-0", "lvm", mount="/", path="/dev/mapper/ubuntu--vg-root")
    runner = _lsblk([
        _dev("nvme0n1", "disk", [_dev("nvme0n1p1", "part", mount="/boot/efi", pkname="nvme0n1"),
                                 _dev("nvme0n1p3", "part", [lv], pkname="nvme0n1")]),
        _dev("nvme1n1", "disk"),
    ])
    verdicts = _verdicts(runner, "/dev/mapper/ubuntu--vg-root")
    assert verdicts["/dev/nvme0n1"] == "protected_root_or_docker"
    assert verdicts["/dev/nvme0n1p3"] == "protected_root_or_docker"
    assert verdicts["/dev/nvme1n1"] == "blank_whole_disk"


def test_every_disk_under_an_mdraid_root_is_protected():
    def member(disk):
        md = _dev("md0", "raid1", mount="/")
        return _dev(disk, "disk", [_dev(f"{disk}1", "part", [md], pkname=disk)])

    runner = _lsblk([member("sda"), member("sdb"), _dev("sdc", "disk")])
    verdicts = _verdicts(runner, "/dev/md0")
    assert verdicts["/dev/sda"] == verdicts["/dev/sdb"] == "protected_root_or_docker"
    assert verdicts["/dev/sdc"] == "blank_whole_disk"


def test_a_disk_with_a_mounted_partition_is_never_offered_for_a_new_partition():
    runner = _lsblk([
        _dev("nvme0n1", "disk", [_dev("nvme0n1p1", "part", mount="/", pkname="nvme0n1")]),
        _dev("nvme1n1", "disk", [_dev("nvme1n1p1", "part", mount="/data", pkname="nvme1n1")]),
    ])
    assert _verdicts(runner, "/dev/nvme0n1p1")["/dev/nvme1n1"] == "unsupported_or_ambiguous"


def test_auto_select_raises_no_safe_target_error_when_nothing_is_safe():
    with pytest.raises(NoSafeTargetError):
        auto_select_target(_runner_factory({}), [], _root_mount(), _docker_mount())


def _filesystem(target, avail_gib, size_gib=500, fstype="ext4", source="/dev/nvme0n1p2", local=True) -> MountedFilesystem:
    return MountedFilesystem(
        target=target, source=source, fstype=fstype, size_bytes=size_gib * 1024**3, avail_bytes=avail_gib * 1024**3, local=local
    )


def _block(path, kernel_name, type, parent=None, transport=None, children=()):
    return BlockDevice(path=path, name=path.rsplit("/", 1)[-1], kernel_name=kernel_name, type=type, size_bytes=1024**4, fstype=None,
                       mountpoints=[], parent_kernel_name=parent, model=None, serial=None, transport=transport, children=list(children))


def test_load_mounted_filesystems_marks_local_block_filesystems_only():
    gib = 1024**3
    findmnt = {
        "filesystems": [
            {"target": "/", "source": "/dev/nvme0n1p2", "fstype": "ext4", "size": 500 * gib, "avail": 300 * gib},
            {"target": "/raid", "source": "/dev/md0", "fstype": "xfs", "size": 4000 * gib, "avail": 3500 * gib},
            {"target": "/mnt/nfs", "source": "nas:/export", "fstype": "nfs4", "size": 9000 * gib, "avail": 8000 * gib},
            {"target": "/media/usb", "source": "/dev/sdb1", "fstype": "ext4", "size": 2000 * gib, "avail": 1900 * gib},
            {"target": "/boot/efi", "source": "/dev/nvme0n1p1", "fstype": "vfat", "size": 1 * gib, "avail": 1 * gib},
        ]
    }
    runner = _runner_factory({
        ("findmnt", "--json", "--bytes", "--list", "--real", "-o", "TARGET,SOURCE,FSTYPE,SIZE,AVAIL"): CommandResult(
            args=[], stdout=json.dumps(findmnt), stderr="", returncode=0
        )
    })
    usb = BlockDevice(path="/dev/sdb", name="sdb", kernel_name="sdb", type="disk", size_bytes=2000 * gib, fstype=None,
                      mountpoints=[], parent_kernel_name=None, model=None, serial=None, transport="usb", children=[
                          BlockDevice(path="/dev/sdb1", name="sdb1", kernel_name="sdb1", type="part", size_bytes=2000 * gib,
                                      fstype="ext4", mountpoints=["/media/usb"], parent_kernel_name="sdb", model=None, serial=None)])
    nvme = _block("/dev/nvme0n1", "nvme0n1", "disk", transport="nvme", children=[
        _block("/dev/nvme0n1p1", "nvme0n1p1", "part", parent="nvme0n1"), _block("/dev/nvme0n1p2", "nvme0n1p2", "part", parent="nvme0n1")])
    raid = _block("/dev/sda", "sda", "disk", transport="sata", children=[_block("/dev/md0", "md0", "raid1", parent="sda")])

    filesystems = load_mounted_filesystems(runner, [usb, nvme, raid])

    assert {fs.target: fs.local for fs in filesystems} == {
        "/": True, "/raid": True, "/mnt/nfs": False, "/media/usb": False, "/boot/efi": False
    }
    assert filesystems[1].avail_bytes == 3500 * gib


def test_plan_loop_file_takes_the_filesystem_with_most_free_space_minus_the_reserve():
    plan = plan_loop_file_target([_filesystem("/", 300), _filesystem("/raid", 3500, size_gib=4000, source="/dev/md0")], "/var/lib/docker")

    assert plan.classification == "loop_file" and plan.selection_mode == "auto"
    assert plan.resolved_target == "/raid/lium-docker.xfs" and plan.loop_file_mountpoint == "/raid"
    assert plan.loop_file_reserve_bytes == 400 * 1024**3   # 10% of 4000 GiB beats the 20 GiB floor
    assert plan.loop_file_size_bytes == 3100 * 1024**3
    assert plan.needs_format is True and plan.needs_partition_creation is False


def test_plan_loop_file_on_root_goes_to_var_lib_and_keeps_at_least_20_gib():
    plan = plan_loop_file_target([_filesystem("/", 170, size_gib=190)], "/var/lib/docker")

    assert plan.resolved_target == "/var/lib/lium-docker.xfs"
    assert plan.loop_file_reserve_bytes == 20 * 1024**3 and plan.loop_file_size_bytes == 150 * 1024**3


def test_plan_loop_file_refuses_under_the_140_gib_listing_floor_with_the_numbers():
    # the file is the disk the node reports: 100 GiB free for the marketplace listing after ~40 GiB of node images
    with pytest.raises(RuntimeError, match="would get 130 GiB \\(150 GiB free, 20 GiB kept.*needs 140 GiB.*100 GiB free"):
        plan_loop_file_target([_filesystem("/", 150, size_gib=180)], "/var/lib/docker")


def test_plan_loop_file_refuses_a_file_under_1_5x_the_gpu_vram():
    with pytest.raises(RuntimeError, match="would get 250 GiB, under 1.5x the 640 GiB of GPU VRAM on this host \\(960 GiB\\)"):
        plan_loop_file_target([_filesystem("/", 300)], "/var/lib/docker", total_vram_bytes=8 * 80 * 1024**3)


def test_plan_loop_file_accepts_a_file_of_1_5x_the_gpu_vram():
    plan = plan_loop_file_target([_filesystem("/", 300)], "/var/lib/docker", total_vram_bytes=2 * 80 * 1024**3)

    assert plan.loop_file_size_bytes == 250 * 1024**3


def test_plan_loop_file_skips_a_docker_root_that_is_its_own_mount():
    filesystems = [_filesystem("/", 200), _filesystem("/var/lib/docker", 900, source="/dev/sdc1")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker")

    assert plan.loop_file_mountpoint == "/"


def test_plan_loop_file_explicit_path_and_size(tmp_path):
    filesystems = [_filesystem("/", 300), _filesystem("/raid", 3500, size_gib=4000, source="/dev/md0")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker", "/raid/pool/docker.xfs", 500 * 1024**3)

    assert plan.selection_mode == "explicit" and plan.loop_file_mountpoint == "/raid"
    assert plan.resolved_target == "/raid/pool/docker.xfs" and plan.loop_file_size_bytes == 500 * 1024**3


@pytest.mark.parametrize(
    "image_file, size_gib, match",
    [
        ("/var/lib/docker/pool.xfs", None, "inside /var/lib/docker"),
        ("/raid/pool.xfs", 3200, "does not fit on /raid"),
        ("/mnt/nfs/pool.xfs", None, "not on a local filesystem"),
    ],
)
def test_plan_loop_file_refuses_bad_explicit_requests(image_file, size_gib, match):
    filesystems = [
        _filesystem("/", 300),
        _filesystem("/raid", 3500, size_gib=4000, source="/dev/md0"),
        _filesystem("/mnt/nfs", 8000, size_gib=9000, fstype="nfs4", source="nas:/export", local=False),
    ]
    with pytest.raises(RuntimeError, match=match):
        plan_loop_file_target(filesystems, "/var/lib/docker", image_file, size_gib * 1024**3 if size_gib else None)


def test_merge_overlay2_switches_off_the_containerd_snapshotter_when_asked():
    merged, changed = merge_overlay2({"storage-driver": "overlay2", "features": {"buildkit": True}}, disable_containerd_snapshotter=True)

    assert changed is True
    assert merged["features"] == {"buildkit": True, "containerd-snapshotter": False}


@pytest.mark.parametrize("image_file", ["/data/$(reboot).xfs", "/data/lium docker.xfs", '/data/a"b.xfs'])
def test_plan_loop_file_target_refuses_paths_unsafe_for_fstab_and_udev(image_file):
    filesystems = [_filesystem("/", 100, size_gib=250), _filesystem("/data", 1500, size_gib=2000, source="/dev/sdb1")]

    with pytest.raises(RuntimeError, match="may only contain letters, digits"):
        plan_loop_file_target(filesystems, "/var/lib/docker", image_file=image_file)


def test_source_disk_is_local_walks_every_ancestor():
    gib = 1024**3
    lvm = BlockDevice(path="/dev/mapper/vg-data", name="vg-data", kernel_name="dm-0", type="lvm", size_bytes=2000 * gib,
                      fstype="ext4", mountpoints=["/data"], parent_kernel_name="sdb1", model=None, serial=None)
    part = BlockDevice(path="/dev/sdb1", name="sdb1", kernel_name="sdb1", type="part", size_bytes=2000 * gib, fstype="LVM2_member",
                       mountpoints=[], parent_kernel_name="sdb", model=None, serial=None, children=[lvm])
    usb = BlockDevice(path="/dev/sdb", name="sdb", kernel_name="sdb", type="disk", size_bytes=2000 * gib, fstype=None,
                      mountpoints=[], parent_kernel_name=None, model=None, serial=None, transport="usb", children=[part])
    nvme_part = BlockDevice(path="/dev/nvme0n1p2", name="nvme0n1p2", kernel_name="nvme0n1p2", type="part", size_bytes=500 * gib,
                            fstype="ext4", mountpoints=["/"], parent_kernel_name="nvme0n1", model=None, serial=None)
    nvme = BlockDevice(path="/dev/nvme0n1", name="nvme0n1", kernel_name="nvme0n1", type="disk", size_bytes=500 * gib, fstype=None,
                       mountpoints=[], parent_kernel_name=None, model=None, serial=None, transport="nvme", children=[nvme_part])

    assert _source_disk_is_local("/dev/mapper/vg-data", [usb, nvme]) is False
    assert _source_disk_is_local("/dev/sdb1", [usb, nvme]) is False
    assert _source_disk_is_local("/dev/nvme0n1p2", [usb, nvme]) is True


@pytest.mark.parametrize("first_parent", ["sata", "usb"])
def test_source_disk_is_local_walks_every_parent_of_a_multi_disk_volume(first_parent):
    # lsblk lists the LV under each physical volume; a USB member makes it non-local whichever listing comes first
    lv_under_sata = _block("/dev/mapper/vg-data", "dm-0", "lvm", parent="sda1")
    lv_under_usb = _block("/dev/mapper/vg-data", "dm-0", "lvm", parent="sdb1")
    sata = _block("/dev/sda", "sda", "disk", transport="sata", children=[_block("/dev/sda1", "sda1", "part", parent="sda", children=[lv_under_sata])])
    usb = _block("/dev/sdb", "sdb", "disk", transport="usb", children=[_block("/dev/sdb1", "sdb1", "part", parent="sdb", children=[lv_under_usb])])
    devices = [sata, usb] if first_parent == "sata" else [usb, sata]

    assert _source_disk_is_local("/dev/mapper/vg-data", devices) is False
    assert _source_disk_is_local("/dev/dm-0", devices) is False


@pytest.mark.parametrize(
    "source, local",
    [("/dev/dm-0", False), ("/dev/mapper/vg-data", False), ("/dev/sdd1", False), ("/dev/sdd", False), ("tank/root", True), ("/dev/nvme0n1p2", True)],
)
def test_source_disk_is_local_matches_kernel_names_and_distrusts_unlisted_dev_paths(source, local):
    usb = _block("/dev/sdb", "sdb", "disk", transport="usb", children=[_block("/dev/mapper/vg-data", "dm-0", "lvm", parent="sdb")])
    nvme = _block("/dev/nvme0n1", "nvme0n1", "disk", transport="nvme", children=[_block("/dev/nvme0n1p2", "nvme0n1p2", "part", parent="nvme0n1")])

    assert _source_disk_is_local(source, [usb, nvme]) is local


def test_plan_loop_file_target_leaves_var_lib_when_var_is_its_own_filesystem():
    filesystems = [_filesystem("/", 800, size_gib=1000), _filesystem("/var", 120, size_gib=200, source="/dev/nvme0n1p3")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker")

    assert plan.resolved_target == "/lium-docker.xfs"
    assert plan.loop_file_mountpoint == "/"
    assert plan.loop_file_size_bytes == 700 * GIB


def test_plan_loop_file_target_refuses_an_existing_auto_path(monkeypatch):
    monkeypatch.setattr(os.path, "lexists", lambda path: path == "/var/lib/lium-docker.xfs")

    with pytest.raises(RuntimeError, match="/var/lib/lium-docker.xfs already exists"):
        plan_loop_file_target([_filesystem("/", 300)], "/var/lib/docker")
