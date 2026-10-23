from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from lium.cli.mine import storage
from lium.cli.mine.docker_config import build_verification_result, inspect_docker_state, load_daemon_json, merge_overlay2
from lium.cli.mine.models import BlockDevice, CommandResult, MountedFilesystem, MountInfo
from lium.cli.mine.storage import (
    NoSafeTargetError,
    _source_disk_is_local,
    auto_select_target,
    load_mounted_filesystems,
    load_mount_info,
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


def test_has_project_quota_option_accepts_aliases():
    assert storage.has_project_quota_option(["rw", "pquota"]) is True
    assert storage.has_project_quota_option(["rw", "prjquota"]) is True
    assert storage.has_project_quota_option(["rw", "prjquota=1"]) is True
    assert storage.has_project_quota_option(["rw", "noquota"]) is False


def test_build_verification_result_accepts_prjquota_mount_option():
    runner = _runner_factory(
        {
            ("docker", "info", "--format", "{{json .}}"): CommandResult(
                args=["docker", "info", "--format", "{{json .}}"],
                stdout=(
                    '{"DockerRootDir":"/var/lib/docker","Driver":"overlay2",'
                    '"DriverStatus":[["Backing Filesystem","xfs"],["Supports d_type","true"]]}'
                ),
                stderr="",
                returncode=0,
            ),
            ("findmnt", "--json", "--target", "/var/lib/docker"): CommandResult(
                args=["findmnt", "--json", "--target", "/var/lib/docker"],
                stdout='{"filesystems":[{"target":"/var/lib/docker","source":"/dev/vdb1","fstype":"xfs","options":"rw,prjquota"}]}',
                stderr="",
                returncode=0,
            ),
        }
    )

    result = build_verification_result(runner)
    project_quota_check = next(check for check in result.checks if check.name == "Mount options include project quota")

    assert result.passed is True
    assert project_quota_check.passed is True
    assert project_quota_check.expected == "contains pquota or prjquota"


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

    filesystems = load_mounted_filesystems(runner, [usb])

    assert {fs.target: fs.local for fs in filesystems} == {
        "/": True, "/raid": True, "/mnt/nfs": False, "/media/usb": False, "/boot/efi": False
    }
    assert filesystems[1].avail_bytes == 3500 * gib


def test_plan_loop_file_takes_the_filesystem_with_most_free_space_minus_the_reserve():
    plan = plan_loop_file_target([_filesystem("/", 300), _filesystem("/raid", 3500, size_gib=4000, source="/dev/md0")], "/var/lib/docker")

    assert plan.classification == "loop_file" and plan.selection_mode == "auto"
    assert plan.resolved_target == "/raid/lium-docker.xfs" and plan.loop_file_filesystem == "/raid"
    assert plan.loop_file_reserve_bytes == 400 * 1024**3   # 10% of 4000 GiB beats the 20 GiB floor
    assert plan.loop_file_size_bytes == 3100 * 1024**3
    assert plan.needs_format is True and plan.needs_partition_creation is False


def test_plan_loop_file_on_root_goes_to_var_lib_and_keeps_at_least_20_gib():
    plan = plan_loop_file_target([_filesystem("/", 150, size_gib=180)], "/var/lib/docker")

    assert plan.resolved_target == "/var/lib/lium-docker.xfs"
    assert plan.loop_file_reserve_bytes == 20 * 1024**3 and plan.loop_file_size_bytes == 130 * 1024**3


def test_plan_loop_file_refuses_under_100_gib_with_the_numbers():
    with pytest.raises(RuntimeError, match="would get 66 GiB \\(86 GiB free, 20 GiB kept"):
        plan_loop_file_target([_filesystem("/", 86, size_gib=120)], "/var/lib/docker")


def test_plan_loop_file_skips_a_docker_root_that_is_its_own_mount():
    filesystems = [_filesystem("/", 150), _filesystem("/var/lib/docker", 900, source="/dev/sdc1")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker")

    assert plan.loop_file_filesystem == "/"


def test_plan_loop_file_explicit_path_and_size(tmp_path):
    filesystems = [_filesystem("/", 300), _filesystem("/raid", 3500, size_gib=4000, source="/dev/md0")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker", "/raid/pool/docker.xfs", 500 * 1024**3)

    assert plan.selection_mode == "explicit" and plan.loop_file_filesystem == "/raid"
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


def test_plan_loop_file_target_leaves_var_lib_when_var_is_its_own_filesystem():
    filesystems = [_filesystem("/", 800, size_gib=1000), _filesystem("/var", 120, size_gib=200, source="/dev/nvme0n1p3")]

    plan = plan_loop_file_target(filesystems, "/var/lib/docker")

    assert plan.resolved_target == "/lium-docker.xfs"
    assert plan.loop_file_filesystem == "/"
    assert plan.loop_file_size_bytes == 700 * GIB


def test_plan_loop_file_target_refuses_an_existing_auto_path(monkeypatch):
    monkeypatch.setattr(os.path, "lexists", lambda path: path == "/var/lib/lium-docker.xfs")

    with pytest.raises(RuntimeError, match="/var/lib/lium-docker.xfs already exists"):
        plan_loop_file_target([_filesystem("/", 300)], "/var/lib/docker")
