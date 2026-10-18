from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from lium.cli.mine import gpu_splitting, storage
from lium.cli.mine.models import (
    BlockDevice,
    CommandResult,
    DockerState,
    MountInfo,
    PreflightResult,
    SetupPlan,
    TargetPlan,
    VerificationCheck,
    VerificationResult,
)
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
    # the cutover recreates the real /var/lib/docker; skip it so the tests do not depend on the host having Docker
    real_mkdir = Path.mkdir
    monkeypatch.setattr(Path, "mkdir", lambda self, *a, **k: None if str(self) == "/var/lib/docker" else real_mkdir(self, *a, **k))
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
