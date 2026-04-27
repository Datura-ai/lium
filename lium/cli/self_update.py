"""Managed binary auto-update support for the Lium CLI."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from lium.cli import ui
from lium.cli.settings import config

DEFAULT_REPO_SLUG = "Datura-ai/lium-cli"
DEFAULT_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
STALE_LOCK_SECONDS = 60 * 60
STATE_FILE_NAME = "self-update.json"
LOCK_FILE_NAME = "self-update.lock"


@dataclass(frozen=True)
class ManagedInstallLayout:
    """Paths for a managed binary install."""

    root_dir: Path
    bin_dir: Path
    cli_symlink: Path
    versions_dir: Path
    current_version: str
    current_binary: Path


@dataclass
class UpdateResult:
    """Outcome of a startup update attempt."""

    checked: bool = False
    updated: bool = False
    skipped_reason: Optional[str] = None
    current_version: Optional[str] = None
    latest_version: Optional[str] = None
    error: Optional[str] = None
    cleaned_versions: Sequence[str] = field(default_factory=tuple)


class UpdateLock:
    """Filesystem lock to avoid concurrent startup updates."""

    def __init__(self, path: Path, stale_after_seconds: int = STALE_LOCK_SECONDS):
        self.path = path
        self.stale_after_seconds = stale_after_seconds
        self.fd: Optional[int] = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)

        if self.path.exists():
            try:
                age = datetime.now(timezone.utc) - datetime.fromtimestamp(
                    self.path.stat().st_mtime,
                    tz=timezone.utc,
                )
                if age > timedelta(seconds=self.stale_after_seconds):
                    self.path.unlink()
            except OSError:
                return False

        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False

        payload = f"{os.getpid()}\n{datetime.now(timezone.utc).isoformat()}\n"
        os.write(self.fd, payload.encode("utf-8"))
        return True

    def release(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "UpdateLock":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def maybe_perform_startup_update() -> UpdateResult:
    """Attempt a managed-binary update without interrupting CLI startup."""

    result = perform_startup_update()

    if result.updated and result.latest_version and result.current_version:
        ui.info(
            f"Updated Lium CLI from {result.current_version} to {result.latest_version}; "
            "the new version will be used on the next launch."
        )
    elif result.error:
        ui.debug(f"Managed binary auto-update skipped after error: {result.error}")

    return result


def perform_startup_update(
    *,
    home: Optional[Path] = None,
    argv0: Optional[str] = None,
    executable: Optional[str] = None,
    now: Optional[datetime] = None,
) -> UpdateResult:
    """Check for and apply a managed-binary update."""

    now = now or datetime.now(timezone.utc)

    if not _auto_update_enabled():
        return UpdateResult(skipped_reason="disabled")

    layout = discover_managed_install(home=home, argv0=argv0, executable=executable)
    if layout is None:
        return UpdateResult(skipped_reason="not-managed")

    result = UpdateResult(current_version=layout.current_version)
    state_path = layout.root_dir / STATE_FILE_NAME
    lock_path = layout.root_dir / LOCK_FILE_NAME

    if not _should_check_for_updates(state_path=state_path, now=now):
        result.skipped_reason = "throttled"
        return result

    with UpdateLock(lock_path) as lock:
        if not lock.acquire():
            result.skipped_reason = "locked"
            return result

        try:
            latest_release_url = resolve_latest_release_url()
            latest_version = normalize_version(
                _extract_release_version(latest_release_url)
            )
            result.checked = True
            result.latest_version = latest_version

            if compare_versions(latest_version, layout.current_version) <= 0:
                _write_state(
                    state_path,
                    {
                        "last_check": now.isoformat(),
                        "current_version": layout.current_version,
                        "latest_version": latest_version,
                    },
                )
                return result

            asset_name = detect_asset_name()
            release_path = release_download_base(
                version=latest_version, release_url=latest_release_url
            )
            binary_url = f"{release_path}/{asset_name}"
            checksum_url = f"{release_path}/checksums.txt"

            with tempfile.TemporaryDirectory(
                prefix="lium-self-update-"
            ) as temp_dir_str:
                temp_dir = Path(temp_dir_str)
                asset_bytes = _download_bytes(binary_url)
                checksums_text = _download_text(checksum_url)
                verify_checksum(
                    asset_name=asset_name,
                    asset_bytes=asset_bytes,
                    checksums_text=checksums_text,
                )

                staged_asset = temp_dir / asset_name
                staged_asset.write_bytes(asset_bytes)
                staged_asset.chmod(0o755)

                install_versioned_binary(
                    layout=layout, version=latest_version, staged_asset=staged_asset
                )
                switch_cli_symlink(layout=layout, version=latest_version)
                cleaned_versions = cleanup_old_versions(
                    layout=layout,
                    keep_versions=(latest_version, layout.current_version),
                )

            result.updated = True
            result.cleaned_versions = tuple(cleaned_versions)
            _write_state(
                state_path,
                {
                    "last_check": now.isoformat(),
                    "last_update": now.isoformat(),
                    "current_version": latest_version,
                    "previous_version": layout.current_version,
                    "latest_version": latest_version,
                    "cleaned_versions": list(cleaned_versions),
                },
            )
            return result
        except Exception as exc:  # noqa: BLE001 - startup must not break CLI execution
            result.error = str(exc)
            _write_state(
                state_path,
                {
                    "last_check": now.isoformat(),
                    "current_version": layout.current_version,
                    "latest_version": result.latest_version,
                    "last_error": result.error,
                },
            )
            return result


def discover_managed_install(
    *,
    home: Optional[Path] = None,
    argv0: Optional[str] = None,
    executable: Optional[str] = None,
) -> Optional[ManagedInstallLayout]:
    """Return the managed install layout for the current process, if any."""

    root_dir = (home or Path.home()) / ".lium"
    cli_symlink = root_dir / "bin" / "lium"
    versions_dir = root_dir / "versions"

    if not cli_symlink.is_symlink():
        return None

    try:
        current_binary = cli_symlink.resolve(strict=True)
    except FileNotFoundError:
        return None

    if current_binary.name != "lium":
        return None

    try:
        relative_binary = current_binary.relative_to(versions_dir)
    except ValueError:
        return None

    if len(relative_binary.parts) != 2:
        return None

    current_version = relative_binary.parts[0]
    candidate_paths = tuple(_candidate_paths(argv0=argv0, executable=executable))

    if candidate_paths:
        symlink_path_str = str(cli_symlink)
        binary_path_str = str(current_binary)
        matches_current_process = any(
            _same_file(path, cli_symlink)
            or _same_file(path, current_binary)
            or str(path) in {symlink_path_str, binary_path_str}
            for path in candidate_paths
        )
        if not matches_current_process:
            return None

    return ManagedInstallLayout(
        root_dir=root_dir,
        bin_dir=root_dir / "bin",
        cli_symlink=cli_symlink,
        versions_dir=versions_dir,
        current_version=current_version,
        current_binary=current_binary,
    )


def detect_asset_name() -> str:
    """Return the release asset name for the current platform."""

    system = os.environ.get("LIUM_INSTALLER_UNAME_S") or os.uname().sysname
    machine = os.environ.get("LIUM_INSTALLER_UNAME_M") or os.uname().machine

    os_name = system.lower()
    if machine in {"x86_64", "amd64"}:
        arch = "amd64"
    elif machine in {"arm64", "aarch64"}:
        arch = "arm64"
    else:
        raise RuntimeError(f"unsupported architecture: {machine}")

    if os_name not in {"linux", "darwin"}:
        raise RuntimeError(f"unsupported platform: {os_name}-{arch}")

    return f"lium-{os_name}-{arch}"


def normalize_version(version: str) -> str:
    """Normalize a version string for comparisons and paths."""

    normalized = version.strip().lstrip("v")
    if not normalized:
        raise RuntimeError("release version could not be determined")
    return normalized


def compare_versions(version_a: str, version_b: str) -> int:
    """Compare semantic-ish versions without adding dependencies."""

    key_a = _version_key(normalize_version(version_a))
    key_b = _version_key(normalize_version(version_b))
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def resolve_latest_release_url() -> str:
    """Resolve the latest release URL or return the configured override."""

    override = os.environ.get("LIUM_INSTALLER_RELEASE_URL")
    if override:
        return override.rstrip("/")

    release_base = f"https://github.com/{os.environ.get('LIUM_REPO_SLUG', DEFAULT_REPO_SLUG)}/releases/latest"
    request = Request(release_base, headers=_request_headers())
    with urlopen(request, timeout=10) as response:
        return response.geturl().rstrip("/")


def release_download_base(*, version: str, release_url: Optional[str] = None) -> str:
    """Return the download base URL for a release version."""

    release_url = (
        release_url or os.environ.get("LIUM_INSTALLER_RELEASE_URL", "")
    ).rstrip("/")
    if release_url and "/tag/" in release_url:
        return release_url.replace("/tag/", "/download/", 1)

    repo_slug = os.environ.get("LIUM_REPO_SLUG", DEFAULT_REPO_SLUG)
    return f"https://github.com/{repo_slug}/releases/download/v{normalize_version(version)}"


def verify_checksum(
    *, asset_name: str, asset_bytes: bytes, checksums_text: str
) -> None:
    """Verify the downloaded asset against the release checksum file."""

    expected_checksum = None
    for line in checksums_text.splitlines():
        parts = line.strip().split()
        if len(parts) >= 2 and parts[-1] == asset_name:
            expected_checksum = parts[0]
            break

    if not expected_checksum:
        raise RuntimeError(f"missing checksum for asset {asset_name}")

    actual_checksum = hashlib.sha256(asset_bytes).hexdigest()
    if actual_checksum != expected_checksum:
        raise RuntimeError(f"checksum verification failed for {asset_name}")


def install_versioned_binary(
    *, layout: ManagedInstallLayout, version: str, staged_asset: Path
) -> Path:
    """Install the staged binary into the managed versions directory."""

    version_dir = layout.versions_dir / version
    version_dir.mkdir(parents=True, exist_ok=True)
    target_binary = version_dir / "lium"
    temp_target = version_dir / ".lium.tmp"
    shutil.copy2(staged_asset, temp_target)
    temp_target.chmod(0o755)
    os.replace(temp_target, target_binary)
    return target_binary


def switch_cli_symlink(*, layout: ManagedInstallLayout, version: str) -> None:
    """Atomically switch the managed CLI symlink to a versioned binary."""

    layout.bin_dir.mkdir(parents=True, exist_ok=True)
    temp_link = layout.bin_dir / ".lium.tmp-link"
    try:
        temp_link.unlink()
    except FileNotFoundError:
        pass
    temp_link.symlink_to(Path("..") / "versions" / version / "lium")
    os.replace(temp_link, layout.cli_symlink)


def cleanup_old_versions(
    *, layout: ManagedInstallLayout, keep_versions: Iterable[str]
) -> Sequence[str]:
    """Delete version directories outside the keep set."""

    keep = {normalize_version(version) for version in keep_versions}
    cleaned = []

    if not layout.versions_dir.exists():
        return cleaned

    for child in layout.versions_dir.iterdir():
        if not child.is_dir():
            continue
        if child.name in keep:
            continue
        try:
            shutil.rmtree(child)
        except OSError:
            continue
        cleaned.append(child.name)

    return sorted(cleaned)


def _auto_update_enabled() -> bool:
    disable_env = os.environ.get("LIUM_SELF_UPDATE_DISABLE")
    if disable_env is not None and _is_truthy(disable_env):
        return False

    configured = config.get("update.auto_check", "true")
    return _is_truthy(configured)


def _candidate_paths(argv0: Optional[str], executable: Optional[str]) -> Iterable[Path]:
    for raw_value in (
        argv0 if argv0 is not None else sys.argv[0],
        executable if executable is not None else sys.executable,
    ):
        if not raw_value:
            continue

        path = Path(raw_value).expanduser()
        if not path.is_absolute():
            if os.sep not in raw_value:
                which_path = shutil.which(raw_value)
                if which_path:
                    path = Path(which_path)
                else:
                    path = Path.cwd() / raw_value
            else:
                path = Path.cwd() / raw_value
        yield path


def _same_file(path_a: Path, path_b: Path) -> bool:
    try:
        return path_a.exists() and path_b.exists() and os.path.samefile(path_a, path_b)
    except OSError:
        return False


def _should_check_for_updates(*, state_path: Path, now: datetime) -> bool:
    interval_seconds = _update_check_interval_seconds()
    if interval_seconds <= 0:
        return True

    state = _read_state(state_path)
    last_check_raw = state.get("last_check")
    if not isinstance(last_check_raw, str):
        return True

    try:
        last_check = datetime.fromisoformat(last_check_raw)
    except ValueError:
        return True

    if last_check.tzinfo is None:
        last_check = last_check.replace(tzinfo=timezone.utc)

    return (now - last_check) >= timedelta(seconds=interval_seconds)


def _update_check_interval_seconds() -> int:
    raw_value = (
        os.environ.get("LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS")
        or config.get("update.check_interval_seconds")
        or str(DEFAULT_CHECK_INTERVAL_SECONDS)
    )
    try:
        return max(0, int(str(raw_value).strip()))
    except ValueError:
        return DEFAULT_CHECK_INTERVAL_SECONDS


def _read_state(path: Path) -> Dict[str, object]:
    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict):
            return payload
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return {}


def _write_state(path: Path, payload: Dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
    except OSError:
        return


def _download_bytes(url: str) -> bytes:
    request = Request(url, headers=_request_headers())
    with urlopen(request, timeout=30) as response:
        return response.read()


def _download_text(url: str) -> str:
    return _download_bytes(url).decode("utf-8")


def _request_headers() -> Dict[str, str]:
    return {
        "Accept": "*/*",
        "User-Agent": "lium-cli-self-update",
    }


def _extract_release_version(release_url: str) -> str:
    trimmed = release_url.rstrip("/")
    parsed = urlparse(trimmed)
    version = parsed.path.rsplit("/", 1)[-1]
    return normalize_version(version)


def _version_key(version: str) -> tuple:
    parts = []
    for chunk in version.replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append((0, int(chunk)))
        else:
            parts.append((1, chunk))
    return tuple(parts)


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    return normalized not in {"0", "false", "no", "off", ""}
