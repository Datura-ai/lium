"""Safety gates of the destructive Docker storage setup (lium#359 review): consent, data-root, target disk, pre-stop."""

from __future__ import annotations

from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.mine import gpu_splitting
from lium.cli.mine.models import BlockDevice, CommandResult, TargetPlan

from test_gpu_splitting_cli import _execution_plan, _no_spinner, _sample_plan, _sample_preflight


def _disk(path="/dev/nvme1n1", size_gib=500, removable=False, transport="nvme") -> BlockDevice:
    return BlockDevice(
        path=path, name=path[5:], kernel_name=path[5:], type="disk", size_bytes=size_gib * 1024**3, fstype=None,
        mountpoints=[], parent_kernel_name=None, model="Samsung PM9A3", serial="S6UK", is_removable=removable,
        transport=transport,
    )


def _target(classification="blank_whole_disk") -> TargetPlan:
    return TargetPlan(
        selection_mode="auto", requested_device=None, resolved_target="/dev/nvme1n1", parent_disk="/dev/nvme1n1",
        classification=classification, needs_partition_creation=True, needs_format=True, existing_fs_type=None,
        selection_reason="Auto-selected", needs_new_gpt=True,
    )


def _wipefs(monkeypatch, stdout=""):
    monkeypatch.setattr(
        gpu_splitting, "run_command", lambda args, check=True, capture=True: CommandResult(args, stdout, "", 0)
    )


# --- consent -----------------------------------------------------------------------------------------------------


def test_setup_yes_without_device_refuses_before_any_change(monkeypatch):
    monkeypatch.setattr(gpu_splitting, "timed_step_status", _no_spinner)
    monkeypatch.setattr(gpu_splitting, "_preflight", _sample_preflight)
    monkeypatch.setattr(gpu_splitting, "_build_setup_plan", lambda preflight, device, *_: _sample_plan())
    executed = []
    monkeypatch.setattr(gpu_splitting, "_execute_setup", executed.append)

    result = CliRunner().invoke(cli, ["gpu-splitting", "setup", "--yes"])

    assert result.exit_code == 1 and "needs --device" in result.output
    assert executed == []


def test_confirm_needs_a_terminal(monkeypatch):
    monkeypatch.setattr(gpu_splitting.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(click.Abort):
        gpu_splitting._confirm_plan(_sample_plan(), yes=False)


@pytest.mark.parametrize("typed, proceeds", [("/dev/nvme1n1", True), ("y", False), ("/dev/nvme0n1", False)])
def test_confirm_proceeds_only_on_the_typed_target_path(monkeypatch, typed, proceeds):
    monkeypatch.setattr(gpu_splitting.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(gpu_splitting.Prompt, "ask", lambda *a, **k: typed)
    if proceeds:
        gpu_splitting._confirm_plan(_sample_plan(), yes=False)
    else:
        with pytest.raises(click.Abort):
            gpu_splitting._confirm_plan(_sample_plan(), yes=False)


# --- plan ----------------------------------------------------------------------------------------------------------


def test_plan_refuses_a_non_default_docker_data_root_before_picking_a_disk(monkeypatch):
    preflight = _sample_preflight()
    preflight.docker_state.docker_root_dir = "/data/docker"
    monkeypatch.setattr(gpu_splitting, "auto_select_target", lambda *a: pytest.fail("picked a disk"))
    with pytest.raises(RuntimeError, match="data-root is /data/docker"):
        gpu_splitting._build_setup_plan(preflight, None)


@pytest.mark.parametrize(
    "disk, match",
    [
        (_disk(removable=True), "removable"),
        (_disk(transport="usb"), "attached over usb"),
        (_disk(size_gib=64), "below the 100 GiB minimum"),
    ],
)
def test_target_check_refuses_removable_remote_and_small_disks(monkeypatch, disk, match):
    _wipefs(monkeypatch)
    preflight = _sample_preflight()
    preflight.block_devices = [disk]
    with pytest.raises(RuntimeError, match=match):
        gpu_splitting._check_target_device(preflight, _target())


def test_target_check_refuses_a_blank_disk_with_an_unreported_signature(monkeypatch):
    _wipefs(monkeypatch, stdout="zfs_member\n")
    preflight = _sample_preflight()
    preflight.block_devices = [_disk()]
    with pytest.raises(RuntimeError, match="zfs_member"):
        gpu_splitting._check_target_device(preflight, _target())


def test_target_check_passes_a_large_local_blank_disk(monkeypatch):
    _wipefs(monkeypatch)
    preflight = _sample_preflight()
    preflight.block_devices = [_disk()]
    gpu_splitting._check_target_device(preflight, _target())


# --- before Docker stops -------------------------------------------------------------------------------------------


def _host(monkeypatch, *, ps=CommandResult([], "", "", 0), used=10 * 1024**3, free=100 * 1024**3):
    commands = []

    def run_command(args, check=True, capture=True):
        commands.append(args)
        if args[:2] == ["docker", "ps"]:
            return ps
        if args[0] == "du":
            return CommandResult(args, f"{used}\t/var/lib/docker\n", "", 0)
        raise AssertionError(f"mutated the host: {args}")

    monkeypatch.setattr(gpu_splitting, "run_command", run_command)
    monkeypatch.setattr(gpu_splitting.shutil, "disk_usage", lambda path: SimpleNamespace(free=free))
    return commands


@pytest.mark.parametrize(
    "kwargs, match",
    [
        ({"ps": CommandResult([], "", "Cannot connect to the Docker daemon", 1)}, "Docker is not responding"),
        ({"ps": CommandResult([], "abc123\n", "", 0)}, "Containers are running"),
        ({"used": 50 * 1024**3, "free": 40 * 1024**3}, "backup of /var/lib/docker needs"),
    ],
)
def test_execute_setup_stops_before_touching_docker(monkeypatch, tmp_path, kwargs, match):
    commands = _host(monkeypatch, **kwargs)
    with pytest.raises(RuntimeError, match=match):
        gpu_splitting._execute_setup(_execution_plan(tmp_path))
    assert ["systemctl", "stop", "docker"] not in commands


def test_pre_stop_checks_pass_with_room_and_no_containers(monkeypatch, tmp_path):
    _host(monkeypatch)
    gpu_splitting._pre_stop_checks(_execution_plan(tmp_path))
