#!/usr/bin/env python3
"""Print the smallest version the next release may carry, read from the changelog.

The version is the git tag (hatch-vcs, pyproject.toml): nothing in the tree says what the next tag has
to be, and v0.0.31 … v0.0.46 were all patch bumps while their windows shipped `### Added` fragments
(DAH-3328). This script reads what the changelog says changed since the previous vX.Y.Z tag and turns
the Keep-a-Changelog headers into the bump SemVer asks for:

    ### Added                                   -> minor
    ### Changed / Fixed / Security / Deprecated -> patch
    ### Removed, or a bullet that starts with
    BREAKING (`- **BREAKING**: …`)              -> major from 1.0.0 on; minor while the major is 0
    no ### header, or one not listed above       -> minor, and the fragment is reported: the guard cannot
                                                   tell a feature from a fix, and 9 of the 51 fragments at
                                                   v0.0.41 had no header and most of them were features

What counts as "changed since the previous tag" — both sources, always:

  1. fragments: `changelog.d/*.md` files (not `changelog.d/README.md`) added, edited or renamed between the
     previous tag and the release commit and still present at the release commit;
  2. folded sections: `## [X.Y.Z]` sections of `CHANGELOG.md` that exist at the release commit and did not
     exist at the previous tag — `scripts/changelog.py` folds the fragments into such a section and deletes
     them, so a release that follows the documented step has no fragment left to diff.

The largest bump wins. With no entry from either source the minimum is the next patch — unless files under
`lium/`, `lium_entry.py` or `pyproject.toml` changed since the previous tag: then the release ships code that
no changelog describes, and `--check` refuses it (a fragment or a folded section fixes that).

Two more rules under `--check`:

  * the previous release is the highest vX.Y.Z tag reachable from the release commit, chosen without looking
    at the candidate (only the candidate tag and other tags on the release commit itself are left out), and
    the release tag must be above it and above every other vX.Y.Z tag in the checkout — release.yml marks
    every release `latest`, so a release never goes backwards, whatever the changelog says;
  * a tag below the minimum is accepted only with `allow-version-skip: <reason>` in the release notes at
    publish time (or --allow-skip) AND the skipped minimum committed in `changelog.d/.deferred_minimum`
    (`--defer <reason>` writes it). That file is a floor for every later release until a tag reaches it, so
    a hotfix patch on top of an unreleased feature cannot make the feature's minor bump disappear.
    Delete the file once a release satisfies it.

usage: next_version.py                        print the previous tag, the entries and the minimum
       next_version.py -q                     print only the minimum version
       next_version.py --since v0.0.40        count the entries since that tag instead of the latest
       next_version.py --defer "<reason>"     write the current minimum to changelog.d/.deferred_minimum
       next_version.py --check v0.0.42 [--release-notes FILE | --allow-skip REASON]
                                              exit 1 when the tag is not above the previous release, is below
                                              the minimum, or ships code with no changelog entry

Exit 0 ok, 1 tag refused, 2 bad input (unparseable or missing tag, empty skip reason, bad deferred file, git failure).
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
FRAGMENT_README = "changelog.d/README.md"  # documentation, never a fragment (scripts/changelog.py skips it too)
CHANGELOG = "CHANGELOG.md"
DEFERRED_MINIMUM = "changelog.d/.deferred_minimum"  # `X.Y.Z` on the first line, `# reason` lines after it
CODE_PATHS = ("lium", "lium_entry.py", "pyproject.toml")  # a change here with no changelog entry is refused
RELEASE_HEADING_RE = re.compile(r"^##\s+\[([^\]]+)\]")  # `## [0.0.42] - 2026-09-14`; `## [Unreleased]` is skipped
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
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

    @classmethod
    def parse_bare(cls, text: str) -> Version | None:
        """`0.1.0` without the v — the form the deferred-minimum file and the CHANGELOG headings use."""
        m = VERSION_RE.match(text.strip())
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


def previous_tag(repo: Path, ref: str, candidate: str | None) -> tuple[str, Version] | None:
    """The highest vX.Y.Z tag reachable from ref, chosen without looking at the candidate; None when there is none.

    Under --check (`candidate` = the checked tag) tags on the release commit itself are left out — the candidate is one
    of them, and a refused tag left there (v0.0.42 next to the v0.1.0 that replaced it) released nothing; any other tag
    there is named in a warning, because hatch-vcs may build it instead. Nothing else is filtered: a tag above the
    candidate stays the previous release, and main() then refuses the candidate for going backwards.
    """
    return _highest(repo, ref, candidate, reachable_only=True)


def latest_tag(repo: Path, ref: str, candidate: str) -> tuple[str, Version] | None:
    """The highest vX.Y.Z tag anywhere in the checkout, tags on the release commit left out: what `latest` points at
    (release.yml marks every release latest, so a tag below it on any branch would move `latest` backwards)."""
    return _highest(repo, ref, candidate, reachable_only=False)


def _highest(repo: Path, ref: str, candidate: str | None, reachable_only: bool) -> tuple[str, Version] | None:
    best: tuple[str, Version] | None = None
    release_commit = git(repo, "rev-parse", f"{ref}^{{commit}}").strip() if candidate is not None else None
    listing = ["tag", "--list", "v*"] + (["--merged", ref] if reachable_only else [])
    for tag in git(repo, *listing).split():
        version = Version.parse(tag)
        if version is None:
            continue
        if release_commit and git(repo, "rev-parse", f"{tag}^{{commit}}").strip() == release_commit:
            if reachable_only and tag != candidate:
                warn(
                    f"tag {tag} also points at the release commit; hatch-vcs may build {version} instead of {candidate} — "
                    f"`gh release delete {tag} --cleanup-tag` first."
                )
            continue
        if best is None or version > best[1]:
            best = (tag, version)
    return best


def fragments_since(repo: Path, tag: str, ref: str) -> list[Fragment]:
    """Fragments added, edited or renamed in tag..ref and still present at ref (a folded fragment is deleted)."""
    # --no-renames: a fragment renamed and edited is a changed fragment, not an R that --diff-filter=AM would drop
    out = git(
        repo, "diff", "--name-only", "-z", "--no-renames", "--diff-filter=AM", f"{tag}..{ref}", "--", FRAGMENT_DIR
    )
    paths = [p for p in out.split("\0") if p.endswith(".md") and p != FRAGMENT_README]
    return [classify(path, git(repo, "show", f"{ref}:{path}")) for path in sorted(paths)]


def show_or_empty(repo: Path, ref: str, path: str) -> str:
    probe = ["git", "-C", str(repo), "show", f"{ref}:{path}"]
    proc = subprocess.run(probe, capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def release_sections(text: str) -> dict[str, str]:
    """`## [X.Y.Z]` heading (lower-cased key) -> the section's text, in file order; `## [Unreleased]` is skipped."""
    sections: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines():
        m = RELEASE_HEADING_RE.match(line)
        if m:
            key = None if m.group(1).strip().lower() == "unreleased" else m.group(1).strip().lower()
            if key is not None:
                sections.setdefault(key, "")
            continue
        if line.startswith("## "):
            key = None
        elif key is not None:
            sections[key] += line + "\n"
    return sections


def changelog_sections_since(repo: Path, tag: str, ref: str) -> list[Fragment]:
    """CHANGELOG.md release sections present at ref and absent at tag — what `scripts/changelog.py` folded."""
    before = release_sections(show_or_empty(repo, tag, CHANGELOG))
    after = release_sections(show_or_empty(repo, ref, CHANGELOG))
    return [classify(f"{CHANGELOG} [{key}]", text) for key, text in after.items() if key not in before]


def code_changed_since(repo: Path, tag: str, ref: str) -> list[str]:
    """Files under CODE_PATHS that differ between tag and ref — code a release ships."""
    out = git(repo, "diff", "--name-only", "-z", f"{tag}..{ref}", "--", *CODE_PATHS)
    return sorted(p for p in out.split("\0") if p)


@dataclass(frozen=True)
class Deferred:
    version: Version
    reason: str


def deferred_minimum(repo: Path, ref: str) -> Deferred | None:
    """The floor committed in changelog.d/.deferred_minimum at ref; None when the file is not there. Exit 2 when
    its first line is not X.Y.Z."""
    text = show_or_empty(repo, ref, DEFERRED_MINIMUM)
    if not text.strip():
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    version = Version.parse_bare(lines[0])
    if version is None:
        fail(f"{DEFERRED_MINIMUM} at {ref}: first line {lines[0]!r} is not X.Y.Z")
        raise SystemExit(2)
    reason = " ".join(line.lstrip("#").strip() for line in lines[1:] if line.startswith("#"))
    return Deferred(version, reason)


def write_deferred(repo: Path, version: Version, reason: str) -> Path:
    path = repo / DEFERRED_MINIMUM
    path.write_text(f"{version}\n# {reason.strip()}\n", encoding="utf-8")
    return path


def minimum(previous: Version, entries: list[Fragment]) -> tuple[Version, int, list[Fragment]]:
    """The smallest next version, the bump it is, and the entries that asked for that bump."""
    bump = max((f.bump for f in entries), default=PATCH)
    deciding = [f for f in entries if f.bump == bump and bump > PATCH]
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
    ap.add_argument(
        "--defer",
        metavar="REASON",
        help=f"write the minimum to {DEFERRED_MINIMUM} so every release until one reaches it is held to it",
    )
    ap.add_argument("-q", "--quiet", action="store_true", help="print only the minimum version (not with --check)")
    args = ap.parse_args(argv)
    if args.quiet and args.check:
        ap.error("-q prints only the version; it does not combine with --check")
    if args.defer is not None and (args.check or not args.defer.strip()):
        ap.error("--defer takes a reason and does not combine with --check")
    repo = Path(args.repo)
    out = sys.stdout

    checked: Version | None = None
    ref = args.ref
    if args.check:
        checked = Version.parse(args.check)
        if checked is None:
            fail(
                f"release tag {args.check!r} is not vX.Y.Z; install.sh and the binary's startup self-update fetch releases/download/v<version>"
            )
            return 2
        if ref == "HEAD":
            ref = args.check

    for name in [ref] + ([args.since] if args.since else []):
        if not is_commit(repo, name):
            fail(f"{name} is not a commit in {repo} (a tag needs `git fetch --tags` first)")
            return 2

    if args.since:
        since_version = Version.parse(args.since)
        if since_version is None:
            fail(f"--since {args.since!r} is not vX.Y.Z")
            return 2
        previous = (args.since, since_version)
    else:
        previous = previous_tag(repo, ref, args.check)
    if previous is None:
        fail(f"no vX.Y.Z tag reachable from {ref}")
        return 2
    prev_tag, prev_version = previous

    fragments = fragments_since(repo, prev_tag, ref)
    sections = changelog_sections_since(repo, prev_tag, ref)
    entries = fragments + sections
    code_changed = code_changed_since(repo, prev_tag, ref) if not entries else []
    changelog_floor, bump, deciding = minimum(prev_version, entries)
    if bump == BREAKING and prev_version.major == 0:
        why = f"{BUMP_NAME[bump]} → minor while the major is 0"
    else:
        why = BUMP_NAME[bump]
    reason_text = (
        "; ".join(f"{f.path} has {', '.join(f.reasons)}" for f in deciding) or "no entry asks for more than a patch"
    )

    deferred = deferred_minimum(repo, ref)
    deferred_stale = deferred is not None and deferred.version <= prev_version
    floor = changelog_floor
    if deferred is not None and not deferred_stale and deferred.version > changelog_floor:
        floor = deferred.version
        why = "deferred minimum"
        reason_text = f"{DEFERRED_MINIMUM} says {deferred.version}" + (f": {deferred.reason}" if deferred.reason else "")

    if args.quiet:
        print(floor, file=out)
        return 0

    print(f"previous tag: {prev_tag}", file=out)
    print(f"fragments since {prev_tag}: {len(fragments)}", file=out)
    for f in fragments:
        print(f"  {f.path}  {BUMP_NAME[f.bump]}  ({', '.join(f.reasons)})", file=out)
    print(f"{CHANGELOG} sections since {prev_tag}: {len(sections)}", file=out)
    for f in sections:
        print(f"  {f.path}  {BUMP_NAME[f.bump]}  ({', '.join(f.reasons)})", file=out)
    if code_changed:
        print(f"code changed since {prev_tag} with no changelog entry: {len(code_changed)} file(s)", file=out)
        for path in code_changed[:10]:
            print(f"  {path}", file=out)
    if deferred is not None and not deferred_stale:
        print(f"deferred minimum: {deferred.version}  ({DEFERRED_MINIMUM}: {deferred.reason or 'no reason given'})", file=out)
    print(f"minimum next version: {floor}  ({why}: {reason_text})", file=out)
    out.flush()  # the report comes before the fragment warnings and the verdict in a CI log (the leftover-tag warning is earlier)
    for f in entries:
        if f.unclassified:
            warn(
                f"{f.path}: {f.unclassified}; counted as a feature (minor). Give it a ### Added / Changed / Fixed header."
            )
    if deferred_stale:
        warn(f"{DEFERRED_MINIMUM} says {deferred.version}, already released by {prev_tag}; delete the file.")

    if args.defer is not None:
        path = write_deferred(repo, floor, args.defer)
        print(f"wrote {floor} to {path}; commit it, then release with allow-version-skip: {args.defer.strip()}", file=out)
        return 0

    if checked is None:
        if code_changed:
            warn(
                f"{len(code_changed)} file(s) under {', '.join(CODE_PATHS)} changed since {prev_tag} with no "
                f"changelog.d fragment and no {CHANGELOG} section; --check refuses a release like this."
            )
        return 0

    latest = latest_tag(repo, ref, args.check)
    if latest is not None and checked <= latest[1]:
        where = "the previous release" if latest[0] == prev_tag else "the latest release"
        fail(
            f"release tag {args.check} is not above {where} {latest[0]}: a release never goes backwards "
            f"(the workflow marks every release latest). Delete the release AND its tag (`gh release delete {args.check} "
            f"--cleanup-tag`) and tag v{latest[1].bumped(PATCH)} or higher."
        )
        return 1

    reason = args.allow_skip
    if reason is None and args.release_notes:
        reason = skip_reason(Path(args.release_notes).read_text(encoding="utf-8"))
    if reason is not None and not reason.strip():
        fail("allow-version-skip needs a reason after the colon")
        return 2

    if code_changed and reason is None:
        fail(
            f"release tag {args.check} ships {len(code_changed)} changed file(s) under {', '.join(CODE_PATHS)} "
            f"since {prev_tag} but no changelog.d fragment and no {CHANGELOG} section describes them "
            f"({', '.join(code_changed[:5])}{', …' if len(code_changed) > 5 else ''}). Add a fragment "
            "(changelog.d/README.md) to the release commit and tag again, or publish the release again with a line "
            "`allow-version-skip: <why there is nothing to tell>` in its notes."
        )
        return 1
    if code_changed:
        warn(f"release tag {args.check} ships code with no changelog entry; allowed by allow-version-skip: {reason.strip()}")

    if checked >= floor:
        if deferred is not None and not deferred_stale and checked >= deferred.version:
            warn(f"{DEFERRED_MINIMUM} ({deferred.version}) is satisfied by {args.check}; delete the file in the next commit.")
        print(f"release tag {args.check} is at or above the changelog minimum {floor}: ok", file=out)
        return 0

    if reason is not None:
        if deferred is None or deferred.version < floor:
            have = f"says {deferred.version}" if deferred else "is not in the tree"
            fail(
                f"release tag {args.check} is below the changelog minimum {floor} and allow-version-skip needs that "
                f"minimum persisted, but {DEFERRED_MINIMUM} at {args.check} {have}. Delete the release AND its tag "
                f"(`gh release delete {args.check} --cleanup-tag`), run `python3 scripts/next_version.py --defer "
                f"'<reason>'`, commit the file, tag again and publish again; every release is then held to {floor} "
                "until a tag reaches it (RELEASING.md)."
            )
            return 1
        warn(
            f"release tag {args.check} is below the changelog minimum {floor}; allowed by allow-version-skip: "
            f"{reason.strip()}; {DEFERRED_MINIMUM} holds the next release to {deferred.version}"
        )
        return 0
    persist = (
        ""
        if deferred is not None and deferred.version >= floor
        else f"commit {DEFERRED_MINIMUM} (`python3 scripts/next_version.py --defer '<reason>'`) and "
    )
    fail(
        f"release tag {args.check} is below the changelog minimum {floor}: {reason_text}.\n"
        f"Delete the release AND its tag (`gh release delete {args.check} --cleanup-tag`; a tag left behind becomes "
        f"the next previous tag and hides this window), then either tag v{floor} and release that, or {persist}"
        "publish the release again with a line `allow-version-skip: <why a lower version is right>` in its notes — the "
        "workflow reads the notes of the publish event, so editing them afterwards changes nothing (RELEASING.md)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
