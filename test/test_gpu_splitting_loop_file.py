"""Single-disk hosts: Docker storage in an XFS image file mounted through a loop device (DAH-5046)."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_gpu_splitting_storage import (
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
            loop_file_free_floor_bytes=140 * GIB,
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


def _host_probes(docker_data_gib: int = 0, vram_mib: list[int] | None = None):
    # the read-only commands the plan runs: `du -sxb <docker-root>` and nvidia-smi (missing when vram_mib is None)
    def run_command(args, check=True, capture=True):
        if args[0] == "du":
            return CommandResult(args, f"{docker_data_gib * GIB}\t{args[-1]}\n", "", 0)
        if args[0] == "nvidia-smi":
            if vram_mib is None:
                raise FileNotFoundError("nvidia-smi")
            return CommandResult(args, "".join(f"{mib}\n" for mib in vram_mib), "", 0)
        raise AssertionError(f"unexpected command while planning: {args}")

    return run_command


def _mounts(plan: SetupPlan, mounted: dict):
    # the Docker root is a plain directory on / (or its own ext4 mount) until `mount <root>` runs, then an XFS loop
    # mount with pquota; once its own mount is unmounted the directory sits on /
    def load_mount_info(runner, target):
        if mounted["query_fails"]:
            raise RuntimeError("findmnt failed")
        if mounted["on_target"]:
            return MountInfo(target=target, source="/dev/loop0", fstype="xfs", options=["rw", "noatime", "prjquota"])
        if mounted["original"]:
            return plan.preflight.docker_mount
        return MountInfo(target="/", source="/dev/nvme0n1p2", fstype="ext4", options=["rw"])

    return load_mount_info


# --- plan ------------------------------------------------------------------------------------------------------


def test_plan_falls_back_to_an_image_file_when_no_disk_is_safe(monkeypatch, tmp_path):
    preflight = _sample_preflight()
    preflight.docker_state.storage_driver = "overlayfs"   # Docker 29 with the containerd image store
    monkeypatch.setattr(gpu_splitting, "auto_select_target", lambda *a: (_ for _ in ()).throw(NoSafeTargetError("none")))
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [
        MountedFilesystem(target="/", source="/dev/nvme0n1p2", fstype="ext4", size_bytes=250 * GIB, avail_bytes=200 * GIB, local=True,
                          mounted_at_boot=True)
    ])
    monkeypatch.setattr(gpu_splitting, "load_daemon_json", lambda *a: {})
    monkeypatch.setattr(gpu_splitting, "_fstab_would_change", lambda *a: True)
    monkeypatch.setattr(gpu_splitting, "run_command", _host_probes(docker_data_gib=20, vram_mib=[24564, 24564]))

    plan = gpu_splitting._build_setup_plan(preflight, None)

    assert plan.target.classification == "loop_file" and plan.target.resolved_target == "/var/lib/lium-docker.xfs"
    assert plan.target.loop_file_size_bytes == 175 * GIB and plan.target.loop_file_reserve_bytes == 25 * GIB
    assert plan.target.loop_file_total_vram_bytes == 2 * 24564 * 1024**2
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


def test_loop_file_target_passes_the_summed_gpu_vram_to_the_plan(monkeypatch):
    preflight = _sample_preflight()
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [])
    monkeypatch.setattr(gpu_splitting, "run_command", _host_probes(docker_data_gib=33, vram_mib=[81559, 81559]))
    monkeypatch.setattr(gpu_splitting, "_node_images_present", lambda mine_dir: False)
    planned = {}
    monkeypatch.setattr(gpu_splitting, "plan_loop_file_target", lambda *args: planned.setdefault("args", args))

    gpu_splitting._loop_file_target(preflight, None, 200)

    assert planned["args"][3:] == (200 * GIB, 2 * 81559 * 1024**2, 33 * GIB, False)


def test_loop_file_target_tells_the_plan_when_the_node_images_are_already_pulled(monkeypatch, tmp_path):
    preflight = _sample_preflight()
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [])
    monkeypatch.setattr(gpu_splitting, "run_command", _host_probes(docker_data_gib=60, vram_mib=[81559]))
    asked = []
    monkeypatch.setattr(gpu_splitting, "_node_images_present", lambda mine_dir: asked.append(mine_dir) or True)
    planned = {}
    monkeypatch.setattr(gpu_splitting, "plan_loop_file_target", lambda *args: planned.setdefault("args", args))

    gpu_splitting._loop_file_target(preflight, None, None, tmp_path / "mine")

    assert planned["args"][5:] == (60 * GIB, True) and asked == [tmp_path / "mine"]


def test_loop_file_target_counts_containerd_store_images_as_still_to_pull(monkeypatch, tmp_path):
    # the switch to overlay2 hides the containerd store, so the node images present there are pulled again
    preflight = _sample_preflight()
    preflight.docker_state.storage_driver = "overlayfs"
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [])
    monkeypatch.setattr(gpu_splitting, "run_command", _host_probes(docker_data_gib=60, vram_mib=[81559]))
    monkeypatch.setattr(gpu_splitting, "_node_images_present", lambda mine_dir: True)
    planned = {}
    monkeypatch.setattr(gpu_splitting, "plan_loop_file_target", lambda *args: planned.setdefault("args", args))

    gpu_splitting._loop_file_target(preflight, None, None, tmp_path / "mine")

    assert planned["args"][5:] == (60 * GIB, False)


@pytest.mark.parametrize("checkout_exists, answer", [(False, False), (True, True), (True, False)])
def test_node_images_present_is_lium_mine_s_own_check_on_its_checkout(monkeypatch, tmp_path, capsys, checkout_exists, answer):
    # the rule stays in one place: without the checkout the images count as still to be pulled and nothing is asked
    from lium.cli.commands import mine

    asked = []
    monkeypatch.setattr(mine, "_node_images_present", lambda executor_dir: asked.append(executor_dir) or answer)
    if checkout_exists:
        (tmp_path / "neurons" / "executor").mkdir(parents=True)

    present = gpu_splitting._node_images_present(tmp_path)

    assert present is (checkout_exists and answer)
    assert asked == ([tmp_path / "neurons" / "executor"] if checkout_exists else [])
    assert ("No `lium mine` checkout" in capsys.readouterr().out) is not checkout_exists


def test_loop_file_target_warns_and_plans_without_vram_when_nvidia_smi_is_missing(monkeypatch, capsys):
    preflight = _sample_preflight()
    monkeypatch.setattr(gpu_splitting, "load_mounted_filesystems", lambda *a: [])
    monkeypatch.setattr(gpu_splitting, "run_command", _host_probes(vram_mib=None))
    planned = {}
    monkeypatch.setattr(gpu_splitting, "plan_loop_file_target", lambda *args: planned.setdefault("args", args))

    gpu_splitting._loop_file_target(preflight, None, None)

    assert planned["args"][4] is None
    assert "GPU VRAM could not be read" in capsys.readouterr().out


def test_plan_refuses_device_together_with_file(monkeypatch):
    with pytest.raises(RuntimeError, match="pass one of them"):
        gpu_splitting._build_setup_plan(_sample_preflight(), "/dev/sdb", image_file="/pool.xfs")


def test_fstab_entry_and_drop_in_for_the_image_file():
    assert gpu_splitting._fstab_entry("/var/lib/lium-docker.xfs", gpu_splitting.LOOP_FILE_FSTAB_OPTIONS) == (
        "/var/lib/lium-docker.xfs /var/lib/docker xfs loop,pquota,noatime,nofail 0 0"
    )
    assert gpu_splitting._docker_drop_in_contents("/var/lib/docker") == "[Unit]\nRequiresMountsFor=/var/lib/docker\n"
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
    mounted: dict
    docker: dict
    loop: dict


def _loop_file_host(
    monkeypatch,
    plan: SetupPlan,
    fail_at: list[str] | None = None,
    and_fail: list[str] | None = None,
    and_fail_from: int = 1,
    interrupt_at: list[str] | None = None,
    interrupt_after: list[str] | None = None,
    free_gib: int = 200,
) -> _Commands:
    # and_fail fails from its and_fail_from-th run on: the rsync branch unmounts the original root once in the flow;
    # interrupt_at is Ctrl+C before the command's effect, interrupt_after once the host has already changed; both hit
    # the command's first run only, the rollback may issue it again
    commands = _Commands()
    mounted = {"on_target": False, "original": True, "query_fails": False}
    docker = {"active": True}
    loop = {"attached": False}   # the loop device backs the image only while it is mounted somewhere
    docker_root = plan.preflight.docker_state.docker_root_dir
    original_source = plan.preflight.docker_mount.source

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args == fail_at or (args == and_fail and commands.count(args) >= and_fail_from):
            raise RuntimeError(f"{args[0]} failed")
        first_run = commands.count(args) == 1
        if args == interrupt_at and first_run:
            raise KeyboardInterrupt
        result = _host_effect(args)
        if args == interrupt_after and first_run:
            raise KeyboardInterrupt
        return result

    def _host_effect(args):
        if args[:2] == ["losetup", "-j"]:
            return CommandResult(args=args, stdout="/dev/loop5\n" if loop["attached"] else "", stderr="", returncode=0)
        if args[0] == "mv" and args[1] == docker_root:
            Path(args[1]).rename(args[2])
        if args[0] == "mv" and args[2] == docker_root:
            Path(args[1]).rename(args[2])
        if args[0] == "rmdir":
            Path(args[1]).rmdir()
        if args == ["systemctl", "stop", "docker"]:
            docker["active"] = False
        if args == ["systemctl", "start", "docker"]:
            docker["active"] = True
        if args == ["mount", docker_root]:
            mounted["on_target"] = True
            loop["attached"] = True
        if args[0] == "mount" and args[-2:] == [original_source, docker_root]:
            mounted["original"] = True
        if args == ["umount", docker_root]:
            if docker["active"]:
                raise RuntimeError("umount: target is busy")
            if mounted["on_target"]:
                mounted["on_target"] = False
                loop["attached"] = False
            else:
                mounted["original"] = False
        return CommandResult(args=args, stdout="", stderr="", returncode=0)

    _patch_execute_setup_dependencies(monkeypatch, run_command, _mounts(plan, mounted))
    monkeypatch.setattr(gpu_splitting, "_pre_stop_checks", lambda *_: None)
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(total=240 * GIB, used=0, free=free_gib * GIB))
    created: list[tuple[str, int]] = []

    def create_image_file(path, size_bytes, on_created=lambda: None):
        created.append((path, size_bytes))
        Path(path).write_bytes(b"")
        on_created()

    monkeypatch.setattr(gpu_splitting, "_create_image_file", create_image_file)
    sysfs: dict[str, str] = {}
    monkeypatch.setattr(gpu_splitting, "_write_sysfs", lambda path, value: sysfs.__setitem__(path, value))
    commands.created = created
    commands.sysfs = sysfs
    commands.mounted = mounted
    commands.docker = docker
    commands.loop = loop
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


@pytest.mark.parametrize("own_mount", [False, True], ids=["rename", "rsync"])
@pytest.mark.parametrize("docker_data_gib, passes", [(37, True), (38, False), (181, False)])
def test_pre_stop_checks_keep_the_listing_floor_free_after_the_restore(monkeypatch, tmp_path, own_mount, docker_data_gib, passes):
    # a 180 GiB file keeps about 177 GiB after XFS metadata; the data restored into it must leave 140 GiB free
    docker_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"]) if own_mount else None
    plan = _loop_file_plan(tmp_path, docker_mount=docker_mount)
    commands = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return CommandResult(args, "", "", 0)
        if args[0] == "du":
            return CommandResult(args, f"{docker_data_gib * GIB}\t{plan.preflight.docker_state.docker_root_dir}\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "_check_image_file_parent", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_same_filesystem", lambda *_: False)
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(total=900 * GIB, used=0, free=800 * GIB))

    if passes:
        gpu_splitting._pre_stop_checks(plan)
    else:
        with pytest.raises(RuntimeError, match=f"once the {docker_data_gib} GiB of Docker data .* move into it.*needs 140 GiB free"):
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
    assert Path(plan.fstab_path).read_text() == f"{image} /var/lib/docker xfs loop,pquota,noatime,nofail 0 0\n"
    assert Path(plan.drop_in_path).read_text() == f"[Unit]\nRequiresMountsFor={docker_root}\n"
    assert f'ATTR{{loop/backing_file}}=="{image}"' in Path(plan.udev_rule_path).read_text()
    assert not Path(plan.backup_dir).exists()


def test_execute_setup_refuses_to_move_a_data_root_with_a_mount_under_it(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    host_run_command = gpu_splitting.run_command

    def run_command(args, check=True, capture=True):
        if args[0] == "findmnt":
            commands.append(args)
            return CommandResult(args, f"/\n/boot/efi\n{docker_root}/volumes/backups\n", "", 0)
        return host_run_command(args, check, capture)

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)

    with pytest.raises(RuntimeError, match=f"mounted under {docker_root}: {docker_root}/volumes/backups.*umount"):
        gpu_splitting._execute_setup(plan)

    assert not any(args[0] == "mv" for args in commands)
    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_execute_setup_rolls_back_on_ctrl_c_and_lets_the_interrupt_through(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    image = plan.target.resolved_target
    host_run_command = gpu_splitting.run_command

    def run_command(args, check=True, capture=True):
        if args[:2] == ["mkfs.xfs", "-f"]:
            raise KeyboardInterrupt
        return host_run_command(args, check, capture)

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._execute_setup(plan)

    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert not Path(plan.backup_dir).exists()
    assert not Path(image).exists()
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_execute_setup_ctrl_c_during_backup_cleanup_keeps_the_finished_migration(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    (Path(docker_root) / "volume-B").write_text("y")

    def rmtree(path):
        # Ctrl+C after the first deletion: the backup is half gone and must never be moved back over the image
        os.unlink(os.path.join(path, "image-layers"))
        raise KeyboardInterrupt

    monkeypatch.setattr(gpu_splitting.shutil, "rmtree", rmtree)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._execute_setup(plan)

    assert Path(plan.target.resolved_target).exists()
    assert Path(plan.fstab_path).read_text().startswith(plan.target.resolved_target)
    assert Path(plan.drop_in_path).exists()
    assert sorted(p.name for p in Path(plan.backup_dir).iterdir()) == ["volume-B"]
    assert ["umount", docker_root] not in commands and ["mv", plan.backup_dir, docker_root] not in commands
    assert commands[-1] == ["systemctl", "start", "docker"]


@pytest.mark.parametrize("own_mount", [False, True], ids=["rename", "rsync"])
@pytest.mark.parametrize("step, after", [
    ("stop", False), ("stop", True), ("rename", False), ("rename", True), ("allocate", False), ("allocate", True),
    ("unmount", False), ("unmount", True), ("mount", False), ("mount", True), ("start", False), ("start", True),
])
def test_execute_setup_ctrl_c_around_each_mutation_puts_the_host_back(monkeypatch, tmp_path, own_mount, step, after):
    # the flag for a mutation is set after its helper returns; an interrupt in that window must roll back the same way
    docker_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"]) if own_mount else None
    plan = _loop_file_plan(tmp_path, docker_mount=docker_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    image = plan.target.resolved_target
    if own_mount and step == "rename":
        pytest.skip("the rsync branch does not rename")
    if not own_mount and step == "unmount":
        pytest.skip("the rename branch has no original mount to unmount")
    command = {
        "stop": ["systemctl", "stop", "docker"], "rename": ["mv", docker_root, plan.backup_dir],
        "unmount": ["umount", docker_root], "mount": ["mount", docker_root], "start": ["systemctl", "start", "docker"],
    }.get(step)
    commands = _loop_file_host(
        monkeypatch, plan, interrupt_at=None if after or not command else command, interrupt_after=command if after else None
    )
    if step == "allocate":
        def create_image_file(path, size_bytes, on_created=lambda: None):
            if after:
                Path(path).write_bytes(b"")
                on_created()
            raise KeyboardInterrupt

        monkeypatch.setattr(gpu_splitting, "_create_image_file", create_image_file)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._execute_setup(plan)

    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert not Path(plan.backup_dir).exists() or own_mount   # the rsync copy stays as the backup it is
    assert not Path(image).exists()
    assert not Path(plan.fstab_path).exists() or image not in Path(plan.fstab_path).read_text()
    if step == "mount" and after:
        assert ["umount", docker_root] in commands
    if step == "start" and after:
        # the daemon came up on the image: rollback stops it before unmounting its storage
        rollback = commands[len(commands) - commands[::-1].index(["systemctl", "start", "docker"], 1):]
        assert rollback.index(["systemctl", "stop", "docker"]) < rollback.index(["umount", docker_root])
    assert commands[-1] == ["systemctl", "start", "docker"]
    assert commands.docker["active"]
    assert commands.mounted["original"] and not commands.mounted["on_target"]


def test_execute_setup_rsync_branch_keeps_docker_down_when_the_mount_table_cannot_be_read_after_an_unmount(monkeypatch, tmp_path):
    # Ctrl+C after `umount` of the original root took effect, then findmnt fails in the rollback: whether /dev/sdc1 is
    # still mounted is unknown, so nothing is remounted, the image stays and Docker is not started on a bare directory
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    host_run_command = gpu_splitting.run_command

    def run_command(args, check=True, capture=True):
        result = host_run_command(args, check, capture)
        if args == ["umount", docker_root]:
            commands.mounted["query_fails"] = True
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    messages = []
    monkeypatch.setattr(gpu_splitting.console, "error", messages.append)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._execute_setup(plan)

    assert ["systemctl", "start", "docker"] not in commands
    assert not any(args[0] == "mount" and args[-1] == docker_root for args in commands)
    assert Path(plan.target.resolved_target).exists()
    assert any(f"findmnt {docker_root}" in message and "/dev/sdc1" in message for message in messages)
    assert any("start docker: skipped" in message for message in messages)


@pytest.mark.parametrize("seen_after_remount, starts", [
    ("/dev/sdc1", True), ("/dev/sdc2", False), (None, False),
], ids=["same-device", "other-device", "findmnt-fails"])
def test_execute_setup_rsync_branch_starts_docker_only_on_the_confirmed_original_filesystem(
    monkeypatch, tmp_path, seen_after_remount, starts
):
    # findmnt canonicalises sources, so a mount made through /dev/disk/by-id/... is seen as /dev/sdc1 both at preflight
    # and after the remount; another device, or no answer, keeps Docker down
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    restore = ["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]
    remount = ["mount", "-t", "ext4", "-o", "rw", "/dev/sdc1", docker_root]
    commands = _loop_file_host(monkeypatch, plan, fail_at=restore)
    host_run_command = gpu_splitting.run_command
    host_load_mount_info = gpu_splitting.load_mount_info

    def run_command(args, check=True, capture=True):
        if args == remount:
            commands.append(args)
            commands.mounted["remounted"] = True
            return CommandResult(args, "", "", 0)
        return host_run_command(args, check, capture)

    def load_mount_info(runner, target):
        if not commands.mounted.get("remounted"):
            return host_load_mount_info(runner, target)
        if seen_after_remount is None:
            raise RuntimeError("findmnt failed")
        return MountInfo(target=target, source=seen_after_remount, fstype="ext4", options=["rw"])

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "load_mount_info", load_mount_info)

    with pytest.raises(RuntimeError, match="rsync failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert remount in commands
    assert (["systemctl", "start", "docker"] in commands) is starts
    assert (f"confirm /dev/sdc1 is mounted at {docker_root}" in str(exc_info.value)) is not starts


_ORIGINAL_CONFIG = {
    "daemon_json": '{"debug": true}\n',
    "fstab": "/dev/sdc1 /var/lib/docker ext4 defaults 0 2\n",
    "drop_in": "[Unit]\nRequiresMountsFor=/srv\n",
    "udev_rule": 'ATTR{loop/backing_file}=="/srv/old.img"\n',
}


@pytest.mark.parametrize("existed", [True, False], ids=["existing", "absent"])
@pytest.mark.parametrize("after", [False, True], ids=["before-write", "after-write"])
@pytest.mark.parametrize("config", list(_ORIGINAL_CONFIG))
def test_execute_setup_ctrl_c_around_each_config_write_restores_the_file(monkeypatch, tmp_path, config, after, existed):
    # the flag for a written file is set after its writer returns; rollback must restore every file it may have changed
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    commands = _loop_file_host(monkeypatch, plan)
    path = Path(getattr(plan, f"{config}_path"))
    if existed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_ORIGINAL_CONFIG[config])
    writer_name = {"daemon_json": "_write_json_atomic", "fstab": "_update_fstab"}.get(config, "_write_text_file")
    writer = getattr(gpu_splitting, writer_name)

    def interrupted_writer(target, payload):
        if Path(target) != path:
            return writer(target, payload)
        if after:
            writer(target, payload)
        raise KeyboardInterrupt

    monkeypatch.setattr(gpu_splitting, writer_name, interrupted_writer)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._execute_setup(plan)

    if existed:
        assert path.read_text() == _ORIGINAL_CONFIG[config]
    else:
        assert not path.exists()
    assert not Path(plan.target.resolved_target).exists()
    assert commands[-1] == ["systemctl", "start", "docker"] and commands.docker["active"]


def test_execute_setup_refuses_the_rename_when_the_backup_path_already_exists(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir
    Path(plan.backup_dir).mkdir()

    with pytest.raises(RuntimeError, match="already exists"):
        gpu_splitting._execute_setup(plan)

    assert not any(args[0] == "mv" for args in commands)
    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert commands[-1] == ["systemctl", "start", "docker"]


@pytest.mark.parametrize("own_mount", [False, True])
def test_execute_setup_masks_docker_from_the_stop_to_the_start_in_both_branches(monkeypatch, tmp_path, own_mount):
    # an enabled docker.service would start on the renamed-away root, or on the new filesystem fstab names from step 7
    # before the data is on it, if the host rebooted in between
    docker_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"]) if own_mount else None
    plan = _loop_file_plan(tmp_path, docker_mount=docker_mount)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir

    gpu_splitting._execute_setup(plan)

    mask, unmask = ["systemctl", "mask", "docker"], ["systemctl", "unmask", "docker"]
    assert commands[:3] == [["systemctl", "stop", "docker"], ["systemctl", "stop", "docker.socket"], mask]
    assert commands.index(["rsync", "-aHAX", "--numeric-ids", f"{plan.backup_dir}/", f"{docker_root}/"]) < commands.index(unmask)
    assert commands[-2:] == [unmask, ["systemctl", "start", "docker"]]
    assert commands.count(mask) == 1


def test_execute_setup_masks_docker_again_when_the_rollback_after_the_unmask_cannot_restore_the_data(monkeypatch, tmp_path):
    # step 10 unmasked and started Docker, the post-restore floor check failed, and the rollback's umount fails: the
    # data root is still the image, so Docker must not come back on it after a reboot either
    plan = _loop_file_plan(tmp_path)
    docker_root = plan.preflight.docker_state.docker_root_dir
    commands = _loop_file_host(monkeypatch, plan, free_gib=70, and_fail=["umount", docker_root])

    with pytest.raises(RuntimeError, match="after the restore") as exc_info:
        gpu_splitting._execute_setup(plan)

    mask, unmask, start = ["systemctl", "mask", "docker"], ["systemctl", "unmask", "docker"], ["systemctl", "start", "docker"]
    assert commands.index(unmask) < commands.index(start) and commands.count(start) == 1
    assert commands[-1] == mask and commands.count(mask) == 2
    assert "docker.service stays masked" in str(exc_info.value)


def test_execute_setup_keeps_docker_masked_when_fstab_would_mount_the_image_over_the_restored_root(monkeypatch, tmp_path):
    # rsync branch, the cutover mount fails and the original filesystem is remounted, but the fstab restore fails: a
    # reboot would mount the empty image over the data, so Docker stays masked and the message names the file
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)
    docker_root = plan.preflight.docker_state.docker_root_dir
    commands = _loop_file_host(monkeypatch, plan, fail_at=["mount", docker_root])
    restore_text_file = gpu_splitting._restore_text_file

    def restore(snapshot):
        if snapshot.path == plan.fstab_path:
            raise OSError("read-only /etc")
        restore_text_file(snapshot)

    monkeypatch.setattr(gpu_splitting, "_restore_text_file", restore)

    with pytest.raises(RuntimeError, match="mount failed") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert commands.mounted["original"] and ["systemctl", "start", "docker"] not in commands
    assert commands[-1] == ["systemctl", "mask", "docker"]
    assert f"{plan.fstab_path} would still mount {plan.target.resolved_target} over {docker_root} at boot" in str(exc_info.value)


def test_execute_setup_rollback_keeps_the_image_when_its_loop_device_is_still_attached(monkeypatch, tmp_path):
    # the validation mount's umount failed: the file is attached to a loop device although the Docker root is not
    # mounted on it; unlinked now it would keep its 180 GiB with no name to find them by
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    docker_root = plan.preflight.docker_state.docker_root_dir

    def mount_validation(*_):
        commands.loop["attached"] = True
        raise RuntimeError("umount: /mnt/lium-docker-validate: target is busy")

    monkeypatch.setattr(gpu_splitting, "_mount_validation", mount_validation)

    with pytest.raises(RuntimeError, match="target is busy") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert Path(plan.target.resolved_target).exists()
    assert f"remove {plan.target.resolved_target}: skipped, still attached to /dev/loop5" in str(exc_info.value)
    assert f"The XFS image file {plan.target.resolved_target} was kept" in str(exc_info.value)
    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert commands[-1] == ["systemctl", "start", "docker"]


def test_execute_setup_rollback_keeps_the_image_when_losetup_cannot_be_asked(monkeypatch, tmp_path):
    # a missing or failing losetup must not end the rollback before Docker is unmasked and started, and must not
    # pass for "detached"
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan)
    host_run_command = gpu_splitting.run_command

    def run_command(args, check=True, capture=True):
        if args[:2] == ["losetup", "-j"]:
            raise FileNotFoundError("losetup")
        return host_run_command(args, check, capture)

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)

    with pytest.raises(RuntimeError, match="losetup") as exc_info:
        gpu_splitting._execute_setup(plan)

    assert Path(plan.target.resolved_target).exists()
    assert "`losetup -j` failed, so whether a loop device still holds it is unknown" in str(exc_info.value)
    assert commands[-2:] == [["systemctl", "unmask", "docker"], ["systemctl", "start", "docker"]]


def test_execute_setup_rolls_back_when_the_restore_leaves_under_the_listing_floor_free(monkeypatch, tmp_path):
    plan = _loop_file_plan(tmp_path)
    commands = _loop_file_host(monkeypatch, plan, free_gib=70)
    docker_root = plan.preflight.docker_state.docker_root_dir

    with pytest.raises(RuntimeError, match="70 GiB free on .* after the restore; the marketplace lists a node only with 100 GiB"):
        gpu_splitting._execute_setup(plan)

    assert (Path(docker_root) / "image-layers").read_text() == "x"
    assert not Path(plan.target.resolved_target).exists()
    assert ["umount", docker_root] in commands and commands[-1] == ["systemctl", "start", "docker"]


def test_create_image_file_removes_the_file_when_preallocation_is_interrupted(monkeypatch, tmp_path):
    path = tmp_path / "lium-docker.xfs"
    owned = []

    def posix_fallocate(fd, offset, size):
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "posix_fallocate", posix_fallocate, raising=False)

    with pytest.raises(KeyboardInterrupt):
        gpu_splitting._create_image_file(str(path), 10 * GIB, lambda: owned.append(str(path)))

    assert owned == [str(path)] and not path.exists()


@pytest.mark.parametrize("total_gib, passes", [(238.94, False), (238.96, True), (239, True)])
def test_mounted_total_is_checked_with_the_validators_rounding(monkeypatch, total_gib, passes):
    # two GPUs of 81559 MiB: 159.3 GiB after the validator's rounding, so the filesystem must report 238.95 GiB
    vram = 2 * 81559 * 1024**2
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(total=int(total_gib * GIB), used=0, free=0))

    if passes:
        gpu_splitting._check_mounted_total_clears_vram_floor("/mnt/lium-docker-validate", vram)
    else:
        with pytest.raises(RuntimeError, match="reports 238.9 GiB, under 1.5x the 159.3 GiB of GPU VRAM"):
            gpu_splitting._check_mounted_total_clears_vram_floor("/mnt/lium-docker-validate", vram)


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
    assert ["systemctl", "unmask", "docker"] not in commands and commands[-1] == ["systemctl", "mask", "docker"]
    assert "docker.service stays masked" in str(exc_info.value) and "systemctl unmask docker && systemctl start docker" in str(exc_info.value)
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
    assert "start docker: skipped" in str(exc_info.value) and "docker.service stays masked" in str(exc_info.value)
    assert ["systemctl", "start", "docker"] not in commands and commands[-1] == ["systemctl", "mask", "docker"]
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


@pytest.mark.parametrize("free_gib, passes", [(300, True), (200, False)])
def test_pre_stop_checks_count_the_image_file_beside_the_backup_copy(monkeypatch, tmp_path, free_gib, passes):
    # 30 GiB of data: 33 GiB + 1 GiB for the copy and the 180 GiB image file side by side = 214 GiB
    own_mount = MountInfo(target=str(tmp_path / "docker"), source="/dev/sdc1", fstype="ext4", options=["rw"])
    plan = _loop_file_plan(tmp_path, docker_mount=own_mount)

    def run_command(args, check=True, capture=True):
        if args[:2] == ["docker", "ps"]:
            return CommandResult(args, "", "", 0)
        if args[0] == "du":
            return CommandResult(args, f"{30 * GIB}\t{plan.preflight.docker_state.docker_root_dir}\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting, "_check_image_file_parent", lambda *_: None)
    monkeypatch.setattr(gpu_splitting, "_same_filesystem", lambda *_: True)
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(free=free_gib * GIB))

    if passes:
        gpu_splitting._pre_stop_checks(plan)
    else:
        with pytest.raises(RuntimeError, match="and the image file needs 214.0 GiB"):
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
