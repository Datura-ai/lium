"""scripts/pr_changelog_check.py: PR CI refuses shipped code with no changelog entry, the way a release would."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pr_changelog_check.py"


def api_item(line: str) -> str:
    """`status<TAB>filename[<TAB>previous_filename[<TAB>patch]]` -> one PR files API object, as ci.yml prints it."""
    status, filename, previous, patch = (line.split("\t", 3) + [None, None])[:4]
    return json.dumps({"status": status, "filename": filename, "previous_filename": previous, "patch": patch})


def run(lines: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="".join(f"{api_item(line)}\n" for line in lines),
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
        # a CHANGELOG.md edit that adds no `## [X.Y.Z]` section: the release counts none
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md\t\t@@ -1 +1 @@\n-# Changelog\n+# Change log"],
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md\t\t@@ -7,0 +8,3 @@\n+## [Unreleased]\n+### Fixed\n+- x"],
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md\t\t@@ -8 +7,0 @@\n-## [0.4.3] - 2025-10-23"],
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md"],  # no patch (the API omits a huge one)
        ["renamed\tlium/new.py\tlium/old.py"],  # a move inside lium/
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
        ["modified\tlium/sdk.py", "modified\tCHANGELOG.md\t\t@@ -7,0 +8,3 @@\n+## [0.4.4] - 2026-11-17\n+### Fixed\n+- x"],
        [
            "modified\t.github/workflows/ci.yml",
            "modified\tREADME.md",
            "added\tscripts/x.py",
            "modified\ttest/t.py",
        ],
        ["modified\tlium-docs/x.md"],  # not under `lium/`
        ["modified\tuv.lock"],  # a dependabot bump here: no shipped path
        [],
    ],
)
def test_an_entry_or_no_shipped_code_passes(lines: list[str]) -> None:
    assert run(lines).returncode == 0


def test_a_file_moved_out_of_lium_counts_its_old_path() -> None:
    result = run(["renamed\tscripts/old_helper.py\tlium/old_helper.py"])

    assert result.returncode == 1
    assert "lium/old_helper.py" in result.stdout


def test_the_ci_step_runs_after_the_suite_with_pipefail() -> None:
    import yaml

    workflow = yaml.safe_load(
        (SCRIPT.parents[1] / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["test"]["steps"]
    names = [s.get("name") for s in steps]
    step = steps[names.index("Changelog entry for shipped code")]

    assert names.index("Changelog entry for shipped code") > names.index(
        "Run the test suite"
    )
    assert (
        step["shell"] == "bash"
    )  # bash -eo pipefail: a failed gh api call fails the step
    assert "!cancelled()" in step["if"]  # still runs when the suite failed
    assert workflow["jobs"]["test"]["permissions"]["pull-requests"] == "read"
    assert "previous_filename" in step["run"] and "patch" in step["run"]  # renames and the CHANGELOG.md diff reach it
