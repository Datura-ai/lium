import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from lium.cli.self_update import (
    STATE_FILE_NAME,
    cleanup_old_versions,
    discover_managed_install,
    perform_startup_update,
)


@contextmanager
def static_file_server(root: Path):
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def write_release(root: Path, version: str, *, checksum_matches: bool = True) -> None:
    release_dir = root / "releases" / "download" / f"v{version}"
    release_dir.mkdir(parents=True, exist_ok=True)

    asset_path = release_dir / "lium-linux-amd64"
    asset_body = f"#!/bin/sh\nprintf 'lium {version}\\n'\n"
    asset_path.write_text(asset_body, encoding="utf-8")
    asset_path.chmod(0o755)

    import hashlib

    digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    if not checksum_matches:
        digest = "0" * len(digest)

    (release_dir / "checksums.txt").write_text(
        f"{digest}  lium-linux-amd64\n",
        encoding="utf-8",
    )


def create_managed_install(
    home: Path, current_version: str, *extra_versions: str
) -> Path:
    root = home / ".lium"
    bin_dir = root / "bin"
    versions_dir = root / "versions"
    bin_dir.mkdir(parents=True, exist_ok=True)

    versions = (current_version, *extra_versions)
    for version in versions:
        version_dir = versions_dir / version
        version_dir.mkdir(parents=True, exist_ok=True)
        binary = version_dir / "lium"
        binary.write_text(f"#!/bin/sh\nprintf 'lium {version}\\n'\n", encoding="utf-8")
        binary.chmod(0o755)

    cli_path = bin_dir / "lium"
    cli_path.symlink_to(Path("..") / "versions" / current_version / "lium")
    return cli_path


def test_perform_startup_update_installs_new_version_and_cleans_old_versions(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    cli_path = create_managed_install(home, "0.1.3", "0.1.2")
    release_root = tmp_path / "release-root"
    write_release(release_root, "0.1.4")

    monkeypatch.setenv("LIUM_UPDATE_AUTO_CHECK", "true")
    monkeypatch.setenv("LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("LIUM_INSTALLER_UNAME_S", "Linux")
    monkeypatch.setenv("LIUM_INSTALLER_UNAME_M", "x86_64")

    with static_file_server(release_root) as base_url:
        monkeypatch.setenv(
            "LIUM_INSTALLER_RELEASE_URL", f"{base_url}/releases/tag/v0.1.4"
        )
        result = perform_startup_update(
            home=home, argv0=str(cli_path), executable=str(cli_path)
        )

    assert result.checked is True
    assert result.updated is True
    assert result.current_version == "0.1.3"
    assert result.latest_version == "0.1.4"
    assert os.readlink(cli_path) == "../versions/0.1.4/lium"
    assert (home / ".lium" / "versions" / "0.1.4" / "lium").exists()
    assert (home / ".lium" / "versions" / "0.1.3" / "lium").exists()
    assert not (home / ".lium" / "versions" / "0.1.2").exists()

    state = json.loads((home / ".lium" / STATE_FILE_NAME).read_text(encoding="utf-8"))
    assert state["current_version"] == "0.1.4"
    assert state["previous_version"] == "0.1.3"


def test_perform_startup_update_aborts_on_checksum_mismatch(
    tmp_path: Path, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    cli_path = create_managed_install(home, "0.1.3")
    release_root = tmp_path / "release-root"
    write_release(release_root, "0.1.4", checksum_matches=False)

    monkeypatch.setenv("LIUM_UPDATE_AUTO_CHECK", "true")
    monkeypatch.setenv("LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("LIUM_INSTALLER_UNAME_S", "Linux")
    monkeypatch.setenv("LIUM_INSTALLER_UNAME_M", "x86_64")

    with static_file_server(release_root) as base_url:
        monkeypatch.setenv(
            "LIUM_INSTALLER_RELEASE_URL", f"{base_url}/releases/tag/v0.1.4"
        )
        result = perform_startup_update(
            home=home, argv0=str(cli_path), executable=str(cli_path)
        )

    assert result.checked is True
    assert result.updated is False
    assert "checksum verification failed" in (result.error or "")
    assert os.readlink(cli_path) == "../versions/0.1.3/lium"
    assert not (home / ".lium" / "versions" / "0.1.4").exists()


def test_perform_startup_update_skips_non_managed_installs(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = home / ".lium" / "bin"
    bin_dir.mkdir(parents=True)
    binary_path = bin_dir / "lium"
    binary_path.write_text("plain-binary", encoding="utf-8")

    monkeypatch.setenv("LIUM_UPDATE_AUTO_CHECK", "true")
    monkeypatch.setenv("LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS", "0")

    result = perform_startup_update(
        home=home, argv0=str(binary_path), executable=str(binary_path)
    )

    assert result.checked is False
    assert result.updated is False
    assert result.skipped_reason == "not-managed"


def test_perform_startup_update_honors_check_interval(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    cli_path = create_managed_install(home, "0.1.3")
    state_path = home / ".lium" / STATE_FILE_NAME
    state_path.write_text(
        json.dumps({"last_check": "2026-04-16T12:00:00+00:00"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("LIUM_UPDATE_AUTO_CHECK", "true")
    monkeypatch.setenv("LIUM_SELF_UPDATE_CHECK_INTERVAL_SECONDS", "3600")

    result = perform_startup_update(
        home=home,
        argv0=str(cli_path),
        executable=str(cli_path),
        now=datetime(2026, 4, 16, 12, 30, tzinfo=timezone.utc),
    )

    assert result.checked is False
    assert result.updated is False
    assert result.skipped_reason == "throttled"


def test_discover_managed_install_accepts_active_versioned_binary_path(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    cli_path = create_managed_install(home, "0.1.3")
    current_binary = home / ".lium" / "versions" / "0.1.3" / "lium"

    layout = discover_managed_install(
        home=home,
        argv0=str(current_binary),
        executable=str(current_binary),
    )

    assert layout is not None
    assert layout.cli_symlink == cli_path
    assert layout.current_version == "0.1.3"


def test_cleanup_old_versions_keeps_requested_versions(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    create_managed_install(home, "0.1.4", "0.1.3", "0.1.2")
    layout = discover_managed_install(
        home=home,
        argv0=str(home / ".lium" / "bin" / "lium"),
        executable=str(home / ".lium" / "bin" / "lium"),
    )

    cleaned = cleanup_old_versions(layout=layout, keep_versions=("0.1.4", "0.1.3"))

    assert cleaned == ["0.1.2"]
    assert (home / ".lium" / "versions" / "0.1.4").exists()
    assert (home / ".lium" / "versions" / "0.1.3").exists()
    assert not (home / ".lium" / "versions" / "0.1.2").exists()
