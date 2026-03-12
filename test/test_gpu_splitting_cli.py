from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.commands import mine as legacy_mine
from lium.cli.mine.models import (
    CandidateEvaluation,
    DockerState,
    MountInfo,
    PreflightResult,
    SetupPlan,
    TargetPlan,
    VerificationCheck,
    VerificationResult,
)
from lium.cli.mine import gpu_splitting


@contextmanager
def _no_spinner(*args, **kwargs):
    yield


def _assert_docs_link_present(output: str):
    normalized = output.replace("\n", "").replace("\r", "").replace("│", "").replace(" ", "")
    expected = gpu_splitting.GPU_SPLITTING_DOC_URL.replace(" ", "")
    assert expected in normalized
    assert normalized.count(expected) == 1


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


def test_mine_help_shows_legacy_leaf_command():
    result = CliRunner().invoke(cli, ["mine", "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output
    assert "--hotkey" in result.output
    assert "gpu-splitting" not in result.output
    assert "executor-setup" not in result.output


def test_gpu_splitting_help_is_top_level():
    result = CliRunner().invoke(cli, ["gpu-splitting", "--help"])

    assert result.exit_code == 0
    assert "Prepare Docker storage for LIUM GPU splitting" in result.output
    assert "check" in result.output
    assert "setup" in result.output
    assert "verify" in result.output


def test_gpu_splitting_check_prints_plan_without_prompt(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(gpu_splitting, "_build_setup_plan", lambda preflight, device: _sample_plan())
    monkeypatch.setattr(
        gpu_splitting,
        "evaluate_candidates",
        lambda runner, devices, root_mount, docker_mount: [
            CandidateEvaluation(
                path="/dev/nvme1n1",
                device_type="disk",
                classification="blank_whole_disk",
                allowed=True,
                details="blank whole disk",
            )
        ],
    )

    result = CliRunner().invoke(cli, ["gpu-splitting", "check"])

    assert result.exit_code == 0
    assert "GPU Splitting Requirements" in result.output
    _assert_docs_link_present(result.output)
    assert "Storage Candidates" in result.output
    assert "GPU Splitting Setup Plan" in result.output
    assert "Proceed?" not in result.output
    assert "create GPT label" in result.output


def test_gpu_splitting_verify_reports_failures(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(
        gpu_splitting,
        "build_verification_result",
        lambda runner, docker_root_dir="/var/lib/docker": VerificationResult(
            checks=[
                VerificationCheck(
                    name="Mount options include project quota",
                    passed=False,
                    actual="defaults",
                    expected="contains pquota or prjquota",
                )
            ]
        ),
    )

    result = CliRunner().invoke(cli, ["gpu-splitting", "verify"])

    assert result.exit_code == 1
    assert "GPU Splitting Requirements" in result.output
    _assert_docs_link_present(result.output)
    assert "FAIL" in result.output
    assert "One or more GPU splitting requirements are not satisfied" in result.output


def test_gpu_splitting_setup_aborts_on_negative_confirmation(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(gpu_splitting, "_build_setup_plan", lambda preflight, device: _sample_plan())
    monkeypatch.setattr(gpu_splitting.Confirm, "ask", lambda *args, **kwargs: False)
    called = {"executed": False}
    monkeypatch.setattr(gpu_splitting, "_execute_setup", lambda plan: called.__setitem__("executed", True))

    result = CliRunner().invoke(cli, ["gpu-splitting", "setup"])

    assert result.exit_code == 0
    assert "GPU Splitting Requirements" in result.output
    _assert_docs_link_present(result.output)
    assert "Aborted before making changes" in result.output
    assert called["executed"] is False


def test_gpu_splitting_setup_yes_prints_plan_then_executes(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(gpu_splitting, "_build_setup_plan", lambda preflight, device: _sample_plan())
    called = {"executed": False}

    def execute(plan):
        called["executed"] = True

    monkeypatch.setattr(gpu_splitting, "_execute_setup", execute)

    result = CliRunner().invoke(cli, ["gpu-splitting", "setup", "--yes"])

    assert result.exit_code == 0
    assert "GPU Splitting Requirements" in result.output
    _assert_docs_link_present(result.output)
    assert "Destructive Actions" in result.output
    assert "create partition on /dev/nvme1n1" in result.output
    assert called["executed"] is True


def test_gpu_splitting_check_reports_precise_explicit_device_error(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(
        gpu_splitting,
        "_build_setup_plan",
        lambda preflight, device: (_ for _ in ()).throw(RuntimeError("Unsafe or unsupported device /dev/sda: protected_root_or_docker")),
    )

    result = CliRunner().invoke(cli, ["gpu-splitting", "check", "--device", "/dev/null"])

    assert result.exit_code == 1
    assert "Unsafe or unsupported device /dev/sda" in result.output
    assert "protected_root_or_docker" in result.output


def test_mine_does_not_route_gpu_splitting_as_subcommand(monkeypatch):
    monkeypatch.setattr(
        legacy_mine,
        "_gather_inputs",
        lambda hotkey, auto: {
            "hotkey": "5F4hQyQ1wY2M6zS4mP7dN8kR2tV9xB3cL6jH1nT5uA7q",
            "internal_port": "8080",
            "external_port": "8080",
            "ssh_port": "2200",
            "ssh_public_port": "",
            "port_range": "",
        },
    )
    monkeypatch.setattr(legacy_mine, "_clone_or_update_repo", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_install_executor_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_check_prereqs", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_setup_executor_env", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_apply_env_overrides", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_start_executor", lambda *args, **kwargs: None)
    monkeypatch.setattr(legacy_mine, "_get_gpu_info", lambda: {"gpu_count": 1, "gpu_type": "H100"})
    monkeypatch.setattr(legacy_mine, "_get_public_ip", lambda: "1.2.3.4")
    forwarded = {}

    def fake_validate(extra_args=None):
        forwarded["args"] = extra_args

    monkeypatch.setattr(legacy_mine, "_validate_executor", fake_validate)
    monkeypatch.setattr(legacy_mine.Path, "absolute", lambda self: self)
    monkeypatch.setattr(legacy_mine.Path, "exists", lambda self: True)

    result = CliRunner().invoke(cli, ["mine", "--auto", "gpu-splitting", "check"])

    assert result.exit_code == 0
    assert forwarded["args"] == ["gpu-splitting", "check"]


def test_detect_new_partition_uses_before_after_diff(monkeypatch):
    responses = iter(
        [
            {
                "blockdevices": [
                    {
                        "path": "/dev/nvme1n1",
                        "type": "disk",
                        "children": [
                            {"path": "/dev/nvme1n1p1", "pkname": "nvme1n1", "type": "part"},
                            {"path": "/dev/nvme1n1p2", "pkname": "nvme1n1", "type": "part"},
                        ],
                    }
                ]
            }
        ]
    )

    def runner(args, check=True, capture=True):
        return gpu_splitting.CommandResult(
            args=args,
            stdout=__import__("json").dumps(next(responses)),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr(gpu_splitting, "run_command", runner)

    new_partition = gpu_splitting._detect_new_partition("/dev/nvme1n1", {"/dev/nvme1n1p1"})

    assert new_partition == "/dev/nvme1n1p2"


def test_preflight_fails_fast_when_docker_info_is_unavailable(monkeypatch):
    monkeypatch.setattr(gpu_splitting.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gpu_splitting.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gpu_splitting, "_detect_distro", lambda: "ubuntu")
    monkeypatch.setattr(gpu_splitting, "_require_binary", lambda _: None)
    monkeypatch.setattr(
        gpu_splitting,
        "run_command",
        lambda args, check=True, capture=True: gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        gpu_splitting,
        "inspect_docker_state",
        lambda runner: DockerState(
            docker_root_dir="/var/lib/docker",
            storage_driver=None,
            backing_filesystem=None,
            supports_d_type=None,
            service_running=False,
            docker_info_available=False,
            docker_info_error="permission denied while trying to connect to docker.sock",
        ),
    )
    monkeypatch.setattr(gpu_splitting, "load_block_devices", lambda runner: [])

    def fake_load_mount_info(runner, target):
        if target == "/":
            return MountInfo(target="/", source="/dev/sda1", fstype="ext4", options=["rw"])
        raise AssertionError(f"Unexpected mount lookup target: {target}")

    monkeypatch.setattr(gpu_splitting, "load_mount_info", fake_load_mount_info)

    with pytest.raises(RuntimeError, match="Unable to read Docker metadata required for gpu-splitting preflight"):
        gpu_splitting._preflight()


def test_mount_validation_accepts_prjquota_alias(monkeypatch, tmp_path):
    validation_mount = tmp_path / "lium-docker-validate"
    commands: list[list[str]] = []

    monkeypatch.setattr(gpu_splitting, "Path", lambda *_: validation_mount)
    monkeypatch.setattr(
        gpu_splitting,
        "run_command",
        lambda args, check=True, capture=True: commands.append(args)
        or gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0),
    )
    monkeypatch.setattr(
        gpu_splitting,
        "load_mount_info",
        lambda runner, target: MountInfo(
            target=target,
            source="/dev/vdb1",
            fstype="xfs",
            options=["rw", "prjquota"],
        ),
    )
    monkeypatch.setattr(gpu_splitting.os, "access", lambda *_: True)

    gpu_splitting._mount_validation("/dev/vdb1", "/var/lib/docker")

    assert commands[0] == ["mount", "-o", "pquota", "/dev/vdb1", str(validation_mount)]
    assert commands[-1] == ["umount", str(validation_mount)]


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


def test_execute_setup_success_starts_docker_once(monkeypatch, tmp_path):
    plan = _execution_plan(tmp_path)
    commands: list[list[str]] = []
    mount_state = {"phase": "root"}

    def load_mount_info(runner, target):
        if mount_state["phase"] == "root":
            return plan.preflight.docker_mount
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
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        if args[:3] == ["blkid", "-s", "UUID"]:
            return gpu_splitting.CommandResult(args=args, stdout="uuid-test\n", stderr="", returncode=0)
        if args == ["mount", "/var/lib/docker"]:
            mount_state["phase"] = "target"
            return gpu_splitting.CommandResult(args=args, stdout="", stderr="", returncode=0)
        raise AssertionError(f"Unexpected command: {args}")

    _patch_execute_setup_dependencies(monkeypatch, run_command, load_mount_info)

    gpu_splitting._execute_setup(plan)

    assert commands.count(["systemctl", "start", "docker"]) == 1
