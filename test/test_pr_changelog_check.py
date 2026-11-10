"""scripts/pr_changelog_check.py: PR CI refuses shipped code with no changelog entry, the way a release would."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pr_changelog_check.py"


def run(lines: list[str], author: str = "someone") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--author", author],
        input="".join(f"{line}\n" for line in lines),
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "lines",
    [
        # the #425 case: CLI code, no fragment
        [
            "modified\tlium/cli/mine/storage.py",
            "modified\ttest/test_gpu_splitting_storage.py",
        ],
        ["modified\tpyproject.toml"],
        [
            "removed\tlium/old.py",
            "removed\tchangelog.d/DAH-1.md",
        ],  # deleting a fragment is no entry
        [
            "modified\tlium_entry.py",
            "modified\tchangelog.d/README.md",
        ],  # the README is no entry
    ],
)
def test_shipped_code_without_an_entry_fails_and_names_the_files(
    lines: list[str],
) -> None:
    result = run(lines)

    assert result.returncode == 1
    assert "::error::" in result.stdout and lines[0].split("\t")[1] in result.stdout


@pytest.mark.parametrize(
    "lines",
    [
        ["modified\tlium/cli/mine/storage.py", "added\tchangelog.d/DAH-5983.md"],
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md"],
        [
            "modified\t.github/workflows/ci.yml",
            "modified\tREADME.md",
            "added\tscripts/x.py",
            "modified\ttest/t.py",
        ],
        ["modified\tlium-docs/x.md"],  # not under `lium/`
        [],
    ],
)
def test_an_entry_or_no_shipped_code_passes(lines: list[str]) -> None:
    assert run(lines).returncode == 0


def test_a_dependabot_bump_needs_no_entry_but_a_person_does() -> None:
    bump = ["modified\tpyproject.toml", "modified\tuv.lock"]

    assert run(bump, author="dependabot[bot]").returncode == 0
    assert (
        run(bump, author="dependabot").returncode == 1
    )  # only the bot account, not a look-alike login
