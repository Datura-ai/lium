#!/usr/bin/env python3
"""PR CI: a PR that changes code a release ships must carry its changelog entry.

`next_version.py --check` refuses a release whose code changed since the last tag with no changelog entry, but only at
release time, after the PR merged (v0.41.9 failed that way after #425). This runs the same rule on the PR: a change
under next_version.CODE_PATHS needs a `changelog.d/<name>.md` fragment (changelog.d/README.md) or a CHANGELOG.md edit
in the same PR. The `no-changelog` label (checked in ci.yml) lets a PR with nothing to tell through; the release then
needs `allow-version-skip`.

stdin: one `<status>\t<path>` line per changed file (the PR files API: added, modified, removed, renamed, …).
Exit 1 and name the code files when the entry is missing.
"""

from __future__ import annotations

import sys
from typing import NamedTuple

from next_version import CODE_PATHS


class ChangedFile(NamedTuple):
    status: str
    path: str


def is_shipped_code(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in CODE_PATHS)


def is_changelog_entry(file: ChangedFile) -> bool:
    if file.status == "removed":
        return False
    if file.path == "CHANGELOG.md":
        return True
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
    return [f.path for f in files if is_shipped_code(f.path)]


def main() -> int:
    files = [
        ChangedFile(*line.split("\t", 1))
        for line in sys.stdin.read().splitlines()
        if "\t" in line
    ]
    code = missing_entry(files)
    if not code:
        print("changelog entry: ok")
        return 0
    shown = ", ".join(code[:5]) + (", …" if len(code) > 5 else "")
    print(
        f"::error::this PR changes {len(code)} file(s) a release ships ({shown}) but adds no changelog.d fragment "
        "and no CHANGELOG.md entry, so the next release would be refused. Add changelog.d/<ticket>.md "
        "(changelog.d/README.md), or label the PR no-changelog when there is nothing to tell and re-run this job."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
