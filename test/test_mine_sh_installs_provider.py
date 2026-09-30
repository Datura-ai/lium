"""mine.sh installs the provider extra: without it the first `lium provider ...` after `lium mine` is CONFIG_MISSING."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

MINE_SH = Path(__file__).resolve().parent.parent / "mine.sh"


def test_mine_sh_installs_lium_with_the_provider_extra():
    installs = re.findall(r"uv tool install\s+(\S+)", MINE_SH.read_text())
    assert installs, "mine.sh no longer installs lium with uv tool"
    assert all(spec.strip("'\"") == "lium.io[provider]" for spec in installs), installs


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_mine_sh_is_valid_bash():
    assert subprocess.run(["bash", "-n", str(MINE_SH)], capture_output=True).returncode == 0
