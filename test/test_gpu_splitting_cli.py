from __future__ import annotations

from contextlib import contextmanager

from click.testing import CliRunner

from lium.cli.cli import cli
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


def test_mine_help_lists_new_subcommands():
    result = CliRunner().invoke(cli, ["mine", "--help"])

    assert result.exit_code == 0
    assert "executor-setup" in result.output
    assert "gpu-splitting" in result.output


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

    result = CliRunner().invoke(cli, ["mine", "gpu-splitting", "check"])

    assert result.exit_code == 0
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
                    name="Mount options include pquota",
                    passed=False,
                    actual="defaults",
                    expected="contains pquota",
                )
            ]
        ),
    )

    result = CliRunner().invoke(cli, ["mine", "gpu-splitting", "verify"])

    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "One or more GPU splitting requirements are not satisfied" in result.output


def test_gpu_splitting_setup_aborts_on_negative_confirmation(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(gpu_splitting, "_build_setup_plan", lambda preflight, device: _sample_plan())
    monkeypatch.setattr(gpu_splitting.Confirm, "ask", lambda *args, **kwargs: False)
    called = {"executed": False}
    monkeypatch.setattr(gpu_splitting, "_execute_setup", lambda plan: called.__setitem__("executed", True))

    result = CliRunner().invoke(cli, ["mine", "gpu-splitting", "setup"])

    assert result.exit_code == 0
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

    result = CliRunner().invoke(cli, ["mine", "gpu-splitting", "setup", "--yes"])

    assert result.exit_code == 0
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

    result = CliRunner().invoke(cli, ["mine", "gpu-splitting", "check", "--device", "/dev/null"])

    assert result.exit_code == 1
    assert "Unsafe or unsupported device /dev/sda" in result.output
    assert "protected_root_or_docker" in result.output


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
