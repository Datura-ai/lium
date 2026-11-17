#!/usr/bin/env python3
"""PR CI: a PR that changes code a release ships must carry its changelog entry.

`next_version.py --check` refuses a release whose code changed since the last tag with no changelog entry, but only at
release time, after the PR merged (v0.41.9 failed that way after #425). This runs the same rule on the PR: a change
under next_version.CODE_PATHS (either side of a rename) needs a `changelog.d/<name>.md` fragment (changelog.d/README.md)
or a new `## [X.Y.Z]` section in CHANGELOG.md in the same PR; the release counts only those, so another CHANGELOG.md
edit (an `## [Unreleased]` line, a typo fix) is no entry. The `no-changelog` label (checked in ci.yml) lets a PR with
nothing to tell through; the release then needs `allow-version-skip`.

stdin: one JSON object per changed file, as the PR files API gives it: `status`, `filename`, and `previous_filename`
(renames) and `patch` (the diff, read for CHANGELOG.md) when present.
Exit 1 and name the code files when the entry is missing.
"""

from __future__ import annotations

import json
import sys
from typing import NamedTuple

from next_version import CODE_PATHS, RELEASE_HEADING_RE


class ChangedFile(NamedTuple):
    status: str
    path: str
    previous_path: str = ""
    patch: str = ""

    @classmethod
    def from_api(cls, item: dict) -> ChangedFile:
        return cls(item["status"], item["filename"], item.get("previous_filename") or "", item.get("patch") or "")


def is_shipped_code(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in CODE_PATHS)


def adds_release_heading(patch_line: str) -> bool:
    m = RELEASE_HEADING_RE.match(patch_line[1:]) if patch_line.startswith("+") else None
    return bool(m) and m.group(1).strip().lower() != "unreleased"


def is_changelog_entry(file: ChangedFile) -> bool:
    if file.status == "removed":
        return False
    if file.path == "CHANGELOG.md":
        return any(adds_release_heading(line) for line in file.patch.splitlines())
    return (
        file.path.startswith("changelog.d/")
        and file.path.count("/") == 1
        and file.path.endswith(".md")
        and file.path != "changelog.d/README.md"
    )


def missing_entry(files: list[ChangedFile]) -> list[str]:
    """The shipped-code files of a PR that has no changelog entry; empty when it has one or ships no code."""
    if any(is_changelog_entry(f) for f in files):
        return []
    return [p for f in files for p in dict.fromkeys((f.path, f.previous_path)) if p and is_shipped_code(p)]


def main() -> int:
    files = [ChangedFile.from_api(json.loads(line)) for line in sys.stdin.read().splitlines() if line.strip()]
    shipped_paths_without_entry = missing_entry(files)
    if not shipped_paths_without_entry:
        print("changelog entry: ok")
        return 0
    shown = ", ".join(shipped_paths_without_entry[:5]) + (", …" if len(shipped_paths_without_entry) > 5 else "")
    print(
        f"::error::this PR changes {len(shipped_paths_without_entry)} file(s) a release ships ({shown}) but adds no "
        "changelog.d fragment and no new ## [X.Y.Z] section in CHANGELOG.md, so the next release would be refused. Add "
        "changelog.d/<ticket>.md (changelog.d/README.md), or label the PR no-changelog when there is nothing to tell and re-run this job."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
