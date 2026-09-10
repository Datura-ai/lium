#!/usr/bin/env python3
"""Print the smallest version the next release may carry, read from changelog.d.

The version is the git tag (hatch-vcs, pyproject.toml): nothing in the tree says what the next tag has
to be, and v0.0.31 … v0.0.41 were all patch bumps while their windows shipped `### Added` fragments
(DAH-3328). This script reads the fragments added or changed under changelog.d/ since the previous
vX.Y.Z tag and turns their Keep-a-Changelog headers into the bump SemVer asks for:

    ### Added                                   -> minor
    ### Changed / Fixed / Security / Deprecated -> patch
    ### Removed, or a bullet that starts with
    BREAKING (`- **BREAKING**: …`)              -> major from 1.0.0 on; minor while the major is 0
    no ### header, or one not listed above       -> minor, and the fragment is reported: the guard cannot
                                                   tell a feature from a fix, and 9 of the 51 fragments at
                                                   v0.0.41 had no header and most of them were features

The largest bump wins; with no fragment the minimum is the next patch. A fragment is counted with every
section it has, whether it was added, edited or renamed since the previous tag.

usage: next_version.py                        print the previous tag, the fragments and the minimum
       next_version.py -q                     print only the minimum version
       next_version.py --since v0.0.40        count the fragments since that tag instead of the latest
       next_version.py --check v0.0.42 [--release-notes FILE | --allow-skip REASON]
                                              exit 1 when the tag is below the minimum, with the tag to use
                                              instead; a line `allow-version-skip: <reason>` in the release
                                              notes at publish time (or --allow-skip) turns that into a
                                              warning, so a hotfix patch on top of unreleased features stays
                                              possible

Exit 0 ok, 1 tag below the minimum, 2 bad input (unparseable or missing tag, empty skip reason, git failure).
Standard library only: the runner's python3 executes it, not the project venv (release.yml).
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

FRAGMENT_DIR = "changelog.d"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")  # the v is part of the tag: `git tag --list v*` and install.sh agree
HEADER_RE = re.compile(r"^###\s+(.+?)\s*$")
# `- BREAKING: …` or `- **BREAKING**: …`; the word inside a sentence ("without breaking the shell") is prose
BREAKING_LINE_RE = re.compile(r"^[-*]\s*(\*\*|__)?BREAKING\b")
SKIP_LINE_RE = re.compile(r"^\s*allow-version-skip\s*:\s*(.*?)\s*$", re.IGNORECASE)

# bump ranks: the largest one among the fragments decides
PATCH, MINOR, BREAKING = 0, 1, 2
BUMP_NAME = {PATCH: "patch", MINOR: "minor", BREAKING: "breaking"}
SECTION_BUMP = {
    "added": MINOR,
    "changed": PATCH,
    "fixed": PATCH,
    "security": PATCH,
    "deprecated": PATCH,
    "removed": BREAKING,
}
UNCLASSIFIED_BUMP = MINOR  # a fragment the guard cannot read is treated as a feature, never as a fix


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, tag: str) -> Version | None:
        m = TAG_RE.match(tag.strip())
        return cls(int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

    def bumped(self, bump: int) -> Version:
        if bump == BREAKING and self.major == 0:
            bump = MINOR  # SemVer 2.0.0 §4: anything may change before 1.0.0; a minor is the signal
        if bump == BREAKING:
            return Version(self.major + 1, 0, 0)
        if bump == MINOR:
            return Version(self.major, self.minor + 1, 0)
        return Version(self.major, self.minor, self.patch + 1)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class Fragment:
    path: str
    bump: int
    reasons: tuple[str, ...]  # the headers / lines that asked for this bump
    unclassified: str | None  # why the guard could not read the fragment, when it could not


def classify(path: str, text: str) -> Fragment:
    """The bump one fragment asks for and why."""
    headers: list[str] = []
    breaking_line = False
    for raw in text.splitlines():
        line = raw.strip()
        header = HEADER_RE.match(line)
        if header:
            if header.group(1) not in headers:
                headers.append(header.group(1))
        elif BREAKING_LINE_RE.search(line):
            breaking_line = True
    known = [(h, SECTION_BUMP[h.lower()]) for h in headers if h.lower() in SECTION_BUMP]
    unknown = [h for h in headers if h.lower() not in SECTION_BUMP]
    unclassified = None
    if unknown:
        unclassified = f"header ### {unknown[0]} is not one of {', '.join(sorted(SECTION_BUMP))}"
    elif not known and not breaking_line:
        unclassified = "no ### header"
    bumps = (
        [b for _, b in known] + ([BREAKING] if breaking_line else []) + ([UNCLASSIFIED_BUMP] if unclassified else [])
    )
    bump = max(bumps)
    reasons = [f"### {h}" for h, b in known if b == bump]
    if breaking_line and bump == BREAKING:
        reasons.append("a BREAKING bullet")
    if unclassified and bump == UNCLASSIFIED_BUMP:
        reasons.append(unclassified + ", counted as a feature")
    return Fragment(path, bump, tuple(reasons), unclassified)


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(f"git {' '.join(args)}: {proc.stderr.strip() or proc.returncode}", file=sys.stderr)
        raise SystemExit(2)
    return proc.stdout


def is_commit(repo: Path, ref: str) -> bool:
    probe = ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0


def previous_tag(repo: Path, ref: str, below: Version | None) -> tuple[str, Version] | None:
    """The highest vX.Y.Z tag reachable from ref; None when there is none.

    Under --check (`below` given) a candidate must be below the checked version and must not sit on the release
    commit itself: a refused tag left there (v0.0.42 next to the v0.1.0 that replaced it) released nothing, and
    taking it as the previous tag would hide every fragment of the window.
    """
    best: tuple[str, Version] | None = None
    release_commit = git(repo, "rev-parse", f"{ref}^{{commit}}").strip() if below is not None else None
    for tag in git(repo, "tag", "--list", "v*", "--merged", ref).split():
        version = Version.parse(tag)
        if version is None or (below is not None and version >= below):
            continue
        if release_commit and git(repo, "rev-parse", f"{tag}^{{commit}}").strip() == release_commit:
            continue
        if best is None or version > best[1]:
            best = (tag, version)
    return best


def fragments_since(repo: Path, tag: str, ref: str) -> list[Fragment]:
    # --no-renames: a fragment renamed and edited is a changed fragment, not an R that --diff-filter=AM would drop
    out = git(
        repo, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", f"{tag}..{ref}", "--", FRAGMENT_DIR
    )
    paths = [p for p in out.split("\0") if p.endswith(".md")]
    return [classify(path, git(repo, "show", f"{ref}:{path}")) for path in sorted(paths)]


def minimum(previous: Version, fragments: list[Fragment]) -> tuple[Version, int, list[Fragment]]:
    """The smallest next version, the bump it is, and the fragments that asked for that bump."""
    bump = max((f.bump for f in fragments), default=PATCH)
    deciding = [f for f in fragments if f.bump == bump and bump > PATCH]
    return previous.bumped(bump), bump, deciding


def fail(message: str) -> None:
    """stderr, and a run annotation when GitHub Actions is running us."""
    print(message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::error::" + message.replace("\n", " "))


def warn(message: str) -> None:
    print("warning: " + message, file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS"):
        print("::warning::" + message)


def skip_reason(notes: str) -> str | None:
    """The reason on the first `allow-version-skip:` line of the release notes; '' when the line has none."""
    for line in notes.splitlines():
        m = SKIP_LINE_RE.match(line)
        if m:
            return m.group(1)
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--repo", default=str(Path(__file__).resolve().parents[1]), help="the git checkout (default: this repo)"
    )
    ap.add_argument("--ref", default="HEAD", help="the commit being released (default HEAD; --check uses the tag)")
    ap.add_argument(
        "--since", metavar="TAG", help="the previous release tag (default: the highest vX.Y.Z tag reachable)"
    )
    ap.add_argument("--check", metavar="TAG", help="exit 1 when this release tag is below the minimum")
    ap.add_argument(
        "--release-notes", metavar="FILE", help="with --check: notes that may carry `allow-version-skip: <reason>`"
    )
    ap.add_argument(
        "--allow-skip", metavar="REASON", help="with --check: accept a tag below the minimum for this reason"
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="print only the minimum version (not with --check)")
    args = ap.parse_args(argv)
    if args.quiet and args.check:
        ap.error("-q prints only the version; it does not combine with --check")
    repo = Path(args.repo)
    out = sys.stdout

    checked: Version | None = None
    ref = args.ref
    if args.check:
        checked = Version.parse(args.check)
        if checked is None:
            print(
                f"release tag {args.check!r} is not vX.Y.Z; install.sh and the binary's startup self-update fetch releases/download/v<version>",
                file=sys.stderr,
            )
            return 2
        if ref == "HEAD":
            ref = args.check

    for name in [ref] + ([args.since] if args.since else []):
        if not is_commit(repo, name):
            print(f"{name} is not a commit in {repo} (a tag needs `git fetch --tags` first)", file=sys.stderr)
            return 2

    if args.since:
        since_version = Version.parse(args.since)
        if since_version is None:
            print(f"--since {args.since!r} is not vX.Y.Z", file=sys.stderr)
            return 2
        previous = (args.since, since_version)
    else:
        previous = previous_tag(repo, ref, checked)
    if previous is None:
        print(f"no vX.Y.Z tag reachable from {ref}" + (f" below {args.check}" if checked else ""), file=sys.stderr)
        return 2
    prev_tag, prev_version = previous

    fragments = fragments_since(repo, prev_tag, ref)
    floor, bump, deciding = minimum(prev_version, fragments)
    if bump == BREAKING and prev_version.major == 0:
        why = f"{BUMP_NAME[bump]} → minor while the major is 0"
    else:
        why = BUMP_NAME[bump]
    reason_text = (
        "; ".join(f"{f.path} has {', '.join(f.reasons)}" for f in deciding) or "no fragment asks for more than a patch"
    )

    if args.quiet and not args.check:
        print(floor, file=out)
        return 0

    print(f"previous tag: {prev_tag}", file=out)
    print(f"fragments since {prev_tag}: {len(fragments)}", file=out)
    for f in fragments:
        print(f"  {f.path}  {BUMP_NAME[f.bump]}  ({', '.join(f.reasons)})", file=out)
    print(f"minimum next version: {floor}  ({why}: {reason_text})", file=out)
    out.flush()  # the report comes before any warning or verdict in a CI log
    for f in fragments:
        if f.unclassified:
            warn(
                f"{f.path}: {f.unclassified}; counted as a feature (minor). Give it a ### Added / Changed / Fixed header."
            )

    if checked is None:
        return 0
    if checked >= floor:
        print(f"release tag {args.check} is at or above the changelog minimum {floor}: ok", file=out)
        return 0

    reason = args.allow_skip
    if reason is None and args.release_notes:
        reason = skip_reason(Path(args.release_notes).read_text(encoding="utf-8"))
    if reason is not None:
        if not reason.strip():
            print("allow-version-skip needs a reason after the colon", file=sys.stderr)
            return 2
        warn(
            f"release tag {args.check} is below the changelog minimum {floor}; allowed by allow-version-skip: {reason.strip()}"
        )
        return 0
    fail(
        f"release tag {args.check} is below the changelog minimum {floor}: {reason_text}.\n"
        f"Delete the release AND its tag (`gh release delete {args.check} --cleanup-tag`; a tag left behind becomes "
        f"the next previous tag and hides this window), then either tag v{floor} and release that, or publish "
        "the release again with a line `allow-version-skip: <why a lower version is right>` in its notes — the "
        "workflow reads the notes of the publish event, so editing them afterwards changes nothing (RELEASING.md)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
