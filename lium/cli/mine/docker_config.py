"""Docker inspection and config merge helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

from .models import CommandResult, DockerState, VerificationCheck, VerificationResult
from .storage import has_project_quota_option, load_mount_info

Runner = Callable[[list[str], bool, bool], CommandResult]


def _driver_status_map(data: dict) -> dict[str, str]:
    raw = data.get("DriverStatus") or []
    parsed: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, list) and len(item) >= 2:
                parsed[str(item[0])] = str(item[1])
    return parsed


def inspect_docker_state(runner: Runner) -> DockerState:
    """Inspect Docker storage metadata, tolerating a stopped daemon.

    Command run:

    - `docker info --format '{{json .}}'`

    The parsed `DriverStatus` fields are later used by `verify` and final setup
    verification to check:

    - storage driver
    - backing filesystem
    - `Supports d_type`
    """
    result = runner(["docker", "info", "--format", "{{json .}}"], False, True)
    if result.returncode != 0 or not (result.stdout or "").strip():
        error_text = (result.stderr or "").strip() or (result.stdout or "").strip()
        if not error_text:
            error_text = f"docker info exited with status {result.returncode}"
        if len(error_text) > 400:
            error_text = f"{error_text[:397]}..."
        return DockerState(
            docker_root_dir="/var/lib/docker",
            storage_driver=None,
            backing_filesystem=None,
            supports_d_type=None,
            service_running=False,
            docker_info_available=False,
            docker_info_error=error_text,
            driver_status={},
        )

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from docker info: {exc}") from exc

    driver_status = _driver_status_map(data)
    supports_d_type = driver_status.get("Supports d_type")
    if supports_d_type is not None:
        supports_d_type = supports_d_type.lower() == "true"

    return DockerState(
        docker_root_dir=data.get("DockerRootDir") or "/var/lib/docker",
        storage_driver=data.get("Driver"),
        backing_filesystem=driver_status.get("Backing Filesystem"),
        supports_d_type=supports_d_type,
        service_running=True,
        docker_info_available=True,
        driver_status=driver_status,
        server_version=data.get("ServerVersion"),
        raw=data,
    )


def load_daemon_json(path: str = "/etc/docker/daemon.json") -> dict:
    """Load `/etc/docker/daemon.json` or return `{}` when absent.

    No commands are run here. Invalid JSON is treated as a hard failure because
    the setup flow is not allowed to overwrite a broken Docker config blindly.
    """
    target = Path(path)
    if not target.exists():
        return {}
    try:
        return json.loads(target.read_text() or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{path} contains invalid JSON: {exc}") from exc


def merge_overlay2(existing: dict) -> tuple[dict, bool]:
    """Merge `storage-driver=overlay2` without dropping unrelated keys.

    No commands are run here. This helper is intentionally narrow so setup only
    changes the single Docker option required by the GPU-splitting storage
    prerequisites.
    """
    merged = dict(existing)
    changed = merged.get("storage-driver") != "overlay2"
    merged["storage-driver"] = "overlay2"
    return merged, changed


def build_verification_result(runner: Runner, docker_root_dir: str = "/var/lib/docker") -> VerificationResult:
    """Build the final host verification table.

    Commands delegated through helpers:

    - `docker info --format '{{json .}}'` via `inspect_docker_state(...)`
    - `findmnt --json --target <docker-root>` via `load_mount_info(...)`

    This is used by both `lium gpu-splitting verify` and the final step of
    `gpu-splitting setup`.
    """
    docker_state = inspect_docker_state(runner)
    docker_mount = load_mount_info(runner, docker_root_dir)

    checks = [
        VerificationCheck(
            name="Docker storage driver",
            passed=docker_state.storage_driver == "overlay2",
            actual=docker_state.storage_driver or "unknown",
            expected="overlay2",
        ),
        VerificationCheck(
            name="Backing filesystem",
            passed=(docker_state.backing_filesystem or "").lower() == "xfs",
            actual=docker_state.backing_filesystem or "unknown",
            expected="xfs",
        ),
        VerificationCheck(
            name="Supports d_type",
            passed=docker_state.supports_d_type is True,
            actual=str(docker_state.supports_d_type).lower() if docker_state.supports_d_type is not None else "unknown",
            expected="true",
        ),
        VerificationCheck(
            name="Docker mountpoint",
            passed=docker_mount.target == docker_root_dir,
            actual=docker_mount.target,
            expected=docker_root_dir,
        ),
        VerificationCheck(
            name="Mount options include project quota",
            passed=has_project_quota_option(docker_mount.options),
            actual=",".join(docker_mount.options),
            expected="contains pquota or prjquota",
        ),
    ]
    return VerificationResult(checks=checks)
