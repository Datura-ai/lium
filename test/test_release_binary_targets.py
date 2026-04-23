import hashlib
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
LIUM_SPEC = REPO_ROOT / "lium.spec"

SUPPORTED_TARGETS = {
    ("Linux", "x86_64"): "lium-linux-amd64",
    ("Linux", "aarch64"): "lium-linux-arm64",
    ("Darwin", "x86_64"): "lium-darwin-amd64",
    ("Darwin", "arm64"): "lium-darwin-arm64",
}

DEFAULT_PLATFORM = ("Linux", "x86_64")
DEFAULT_ASSET = SUPPORTED_TARGETS[DEFAULT_PLATFORM]


def run_bash(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-lc", command],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def run_detect_asset_name(uname_s: str, uname_m: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "LIUM_INSTALLER_UNAME_S": uname_s,
            "LIUM_INSTALLER_UNAME_M": uname_m,
        }
    )
    return run_bash("source scripts/install.sh && detect_asset_name", env=env)


def write_fake_curl(bin_dir: Path) -> None:
    fake_curl = bin_dir / "curl"
    fake_curl.write_text(
        """#!/bin/sh
set -eu

output=""
url=""

while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            output="$2"
            shift 2
            ;;
        -f|-s|-S|-L)
            shift
            ;;
        *)
            url="$1"
            shift
            ;;
    esac
done

if [ -z "$output" ] || [ -z "$url" ]; then
    echo "unexpected curl invocation: $*" >&2
    exit 1
fi

asset_name=$(basename "$url")
cp "${LIUM_TEST_RELEASES}/${asset_name}" "$output"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)


def make_fake_release(tmp_path: Path, version: str) -> Path:
    release_dir = tmp_path / f"release-{version}"
    release_dir.mkdir()

    asset_path = release_dir / DEFAULT_ASSET
    asset_body = (
        "#!/bin/sh\n"
        f"printf 'lium {version}\\n'\n"
    )
    asset_path.write_text(asset_body, encoding="utf-8")
    asset_path.chmod(0o755)

    digest = hashlib.sha256(asset_path.read_bytes()).hexdigest()
    (release_dir / "checksums.txt").write_text(
        f"{digest}  {DEFAULT_ASSET}\n",
        encoding="utf-8",
    )

    return release_dir


def make_install_env(home_dir: Path, release_dir: Path, version: str) -> dict[str, str]:
    fake_bin = home_dir / "fake-bin"
    fake_bin.mkdir(exist_ok=True)
    write_fake_curl(fake_bin)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "PATH": f"{fake_bin}:{env['PATH']}",
            "LIUM_INSTALL_DIR": str(home_dir / ".lium" / "bin"),
            "LIUM_INSTALLER_UNAME_S": DEFAULT_PLATFORM[0],
            "LIUM_INSTALLER_UNAME_M": DEFAULT_PLATFORM[1],
            "LIUM_TEST_RELEASES": str(release_dir),
            "LIUM_VERSION": version,
            "SHELL": "/bin/bash",
        }
    )
    return env


def test_install_script_supports_every_released_binary_target():
    for detected_platform, expected_asset in SUPPORTED_TARGETS.items():
        result = run_detect_asset_name(*detected_platform)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected_asset


def test_install_script_rejects_unsupported_targets():
    result = run_detect_asset_name("Linux", "riscv64")

    assert result.returncode != 0
    assert "unsupported architecture: riscv64" in result.stderr


def test_install_script_resolves_version_from_release_url_override():
    env = os.environ.copy()
    env["LIUM_INSTALLER_RELEASE_URL"] = (
        "https://github.com/Datura-ai/lium-cli/releases/tag/v0.1.3"
    )

    result = run_bash("source scripts/install.sh && resolve_version", env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.3"


def test_release_workflow_mentions_every_supported_binary_target():
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    for asset_name in SUPPORTED_TARGETS.values():
        assert f"name: {asset_name}" in workflow_text
        assert f"{asset_name}.sha256" in workflow_text
        assert asset_name in workflow_text


def test_binary_packaging_does_not_depend_on_pywry():
    assert "pywry" not in PYPROJECT.read_text(encoding="utf-8")
    assert "pywry" not in LIUM_SPEC.read_text(encoding="utf-8")


def test_install_script_fresh_install_uses_versioned_symlink_layout(tmp_path: Path):
    version = "0.1.3"
    release_dir = make_fake_release(tmp_path, version)
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    result = run_bash("bash scripts/install.sh", env=make_install_env(home_dir, release_dir, version))

    cli_path = home_dir / ".lium" / "bin" / "lium"
    versioned_binary = home_dir / ".lium" / "versions" / version / "lium"

    assert result.returncode == 0, result.stderr
    assert cli_path.is_symlink()
    assert os.readlink(cli_path) == f"../versions/{version}/lium"
    assert versioned_binary.exists()
    assert versioned_binary.read_text(encoding="utf-8").startswith("#!/bin/sh")
    assert f"Managed binary location: {versioned_binary}" in result.stdout


def test_install_script_reinstall_same_version_is_idempotent(tmp_path: Path):
    version = "0.1.3"
    release_dir = make_fake_release(tmp_path, version)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    env = make_install_env(home_dir, release_dir, version)

    first = run_bash("bash scripts/install.sh", env=env)
    second = run_bash("bash scripts/install.sh", env=env)

    cli_path = home_dir / ".lium" / "bin" / "lium"
    versioned_binary = home_dir / ".lium" / "versions" / version / "lium"

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert cli_path.is_symlink()
    assert os.readlink(cli_path) == f"../versions/{version}/lium"
    assert versioned_binary.exists()


def test_install_script_updates_symlink_for_newer_versions(tmp_path: Path):
    home_dir = tmp_path / "home"
    home_dir.mkdir()

    first_version = "0.1.3"
    first_release = make_fake_release(tmp_path, first_version)
    first_result = run_bash(
        "bash scripts/install.sh",
        env=make_install_env(home_dir, first_release, first_version),
    )

    second_version = "0.1.4"
    second_release = make_fake_release(tmp_path, second_version)
    second_result = run_bash(
        "bash scripts/install.sh",
        env=make_install_env(home_dir, second_release, second_version),
    )

    cli_path = home_dir / ".lium" / "bin" / "lium"

    assert first_result.returncode == 0, first_result.stderr
    assert second_result.returncode == 0, second_result.stderr
    assert cli_path.is_symlink()
    assert os.readlink(cli_path) == f"../versions/{second_version}/lium"
    assert (home_dir / ".lium" / "versions" / first_version / "lium").exists()
    assert (home_dir / ".lium" / "versions" / second_version / "lium").exists()


def test_install_script_refuses_existing_regular_file_install(tmp_path: Path):
    version = "0.1.3"
    release_dir = make_fake_release(tmp_path, version)
    home_dir = tmp_path / "home"
    cli_path = home_dir / ".lium" / "bin" / "lium"
    cli_path.parent.mkdir(parents=True)
    cli_path.write_text("legacy-binary", encoding="utf-8")

    result = run_bash("bash scripts/install.sh", env=make_install_env(home_dir, release_dir, version))

    assert result.returncode != 0
    assert "already exists as a regular file" in result.stderr
    assert "Managed symlink installs are only used for fresh installs." in result.stderr
    assert not cli_path.is_symlink()
    assert cli_path.read_text(encoding="utf-8") == "legacy-binary"
