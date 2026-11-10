#!/usr/bin/env python3
"""PR CI: a PR that changes code a release ships must carry its changelog entry.

`next_version.py --check` refuses a release whose code changed since the last tag with no changelog entry, but only at
release time, after the PR merged (v0.41.9 failed that way after #425). This runs the same rule on the PR: a change
under next_version.CODE_PATHS needs a `changelog.d/<name>.md` fragment (changelog.d/README.md) or a CHANGELOG.md edit
in the same PR. Dependabot's PRs (`--author dependabot[bot]`) are routine dependency bumps and pass without one; the
`no-changelog` label lets any other PR with nothing to tell through (the release then needs `allow-version-skip`).

stdin: one `<status>\t<path>` line per changed file (the PR files API: added, modified, removed, renamed, …).
Exit 1 and name the code files when the entry is missing.
"""

from __future__ import annotations

import argparse
import sys

from next_version import CODE_PATHS

EXEMPT_AUTHORS = frozenset({"dependabot[bot]"})


def is_code(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in CODE_PATHS)


def is_entry(status: str, path: str) -> bool:
    if status == "removed":
        return False
    if path == "CHANGELOG.md":
        return True
    return (
        path.startswith("changelog.d/")
        and path.count("/") == 1
        and path.endswith(".md")
        and path != "changelog.d/README.md"
    )


def missing_entry(files: list[tuple[str, str]]) -> list[str]:
    """The code files of a PR that has no changelog entry; empty when it has one or changes no shipped code."""
    if any(is_entry(status, path) for status, path in files):
        return []
    return [path for _, path in files if is_code(path)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--author", default="", help="the PR author's login; dependabot[bot] is exempt"
    )
    if ap.parse_args().author in EXEMPT_AUTHORS:
        print("changelog entry: not needed for a dependabot[bot] PR")
        return 0
    files = [
        tuple(line.split("\t", 1))
        for line in sys.stdin.read().splitlines()
        if "\t" in line
    ]
    code = missing_entry(files)  # type: ignore[arg-type]
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
