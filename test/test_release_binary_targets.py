import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SUPPORTED_TARGETS = {
    ("Linux", "x86_64"): "lium-linux-amd64",
    ("Linux", "aarch64"): "lium-linux-arm64",
    ("Darwin", "x86_64"): "lium-darwin-amd64",
    ("Darwin", "arm64"): "lium-darwin-arm64",
}


def run_detect_asset_name(uname_s: str, uname_m: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "LIUM_INSTALLER_UNAME_S": uname_s,
            "LIUM_INSTALLER_UNAME_M": uname_m,
        }
    )
    return subprocess.run(
        ["bash", "-lc", "source scripts/install.sh && detect_asset_name"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_install_script_supports_every_released_binary_target():
    for detected_platform, expected_asset in SUPPORTED_TARGETS.items():
        result = run_detect_asset_name(*detected_platform)

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected_asset


def test_install_script_rejects_unsupported_targets():
    result = run_detect_asset_name("Linux", "riscv64")

    assert result.returncode != 0
    assert "unsupported architecture: riscv64" in result.stderr


def test_release_workflow_mentions_every_supported_binary_target():
    workflow_text = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    for asset_name in SUPPORTED_TARGETS.values():
        assert f"name: {asset_name}" in workflow_text
        assert f"{asset_name}.sha256" in workflow_text
        assert asset_name in workflow_text
