#!/usr/bin/env python3
"""Fold changelog.d/*.md fragments into CHANGELOG.md under a new version heading (Keep a Changelog layout).

    python scripts/changelog.py --version 0.0.38            # writes CHANGELOG.md, deletes the fragments
    python scripts/changelog.py --version 0.0.38 --dry-run  # prints the new section only

Each fragment holds `### Added|Changed|Deprecated|Removed|Fixed|Security` headings followed by bullets (see
changelog.d/README.md). Same-named sections are merged in the canonical order; bullets keep the order of the fragment
file names. Lines outside a section go under `### Changed`. The new section goes above the newest released version and
below a `## [Unreleased]` block when CHANGELOG.md has one, so the result does not depend on merge order.
"""
import argparse, datetime, pathlib, re, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SECTIONS = ["Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
# A release heading: `## [0.0.38] - 2026-09-09`. `## [Unreleased]` is not one.
RELEASE_HEADING = re.compile(r"^## \[(?!unreleased\])", re.M | re.I)
UNRELEASED_HEADING = re.compile(r"^## \[unreleased\]", re.M | re.I)


def collect(frag_dir: pathlib.Path) -> tuple[list[pathlib.Path], dict[str, list[str]]]:
    """The fragment files in name order, and the bullets of every non-empty section keyed by section name."""
    sections: dict[str, list[str]] = {s: [] for s in SECTIONS}
    files = sorted(p for p in frag_dir.glob("*.md") if p.name != "README.md")
    for f in files:
        current = "Changed"
        seen_heading = False
        for line in f.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^###\s+(\w+)", line)
            if m:
                seen_heading = True
                name = m.group(1).capitalize()
                if name not in SECTIONS:
                    print(f"{f.name}: unknown section '### {m.group(1)}' filed under Changed", file=sys.stderr)
                current = name if name in SECTIONS else "Changed"
                continue
            if re.match(r"^##\s", line) or not line.strip():
                continue
            if not seen_heading:
                print(f"{f.name}: no '### Added|Changed|…' heading; its bullets go under Changed", file=sys.stderr)
                seen_heading = True  # one warning per file
            sections[current].append(line.rstrip())
    return files, {k: v for k, v in sections.items() if v}


def render(version: str, date: str, sections: dict[str, list[str]]) -> str:
    out = [f"## [{version}] - {date}", ""]
    for name in SECTIONS:
        if name in sections:
            out.append(f"### {name}")
            out.extend(sections[name])
            out.append("")
    return "\n".join(out)


def insert_release(text: str, section: str) -> str:
    """Put `section` above the first released version heading, below any `## [Unreleased]` block."""
    m = RELEASE_HEADING.search(text)
    if m:
        return text[: m.start()] + section + "\n" + text[m.start():]
    if text and not text.endswith("\n"):
        text += "\n"
    return text + "\n" + section + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--version", required=True, help="version heading to write, e.g. 0.0.38")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--root", type=pathlib.Path, default=ROOT, help=argparse.SUPPRESS)  # tests point it at a copy
    a = ap.parse_args(argv)
    frag_dir = a.root / "changelog.d"
    files, sections = collect(frag_dir)
    if not sections:
        sys.exit("changelog.d/ has no fragments")
    section = render(a.version, a.date, sections)
    path = a.root / "CHANGELOG.md"
    text = path.read_text(encoding="utf-8")
    # Both checks run before --dry-run returns, so a preview shows the same refusal and warning as the real fold.
    if re.search(rf"^## \[{re.escape(a.version)}\]", text, re.M):
        sys.exit(f"CHANGELOG.md already has a [{a.version}] section")
    if UNRELEASED_HEADING.search(text):
        print("CHANGELOG.md has an `## [Unreleased]` block; its bullets are NOT part of "
              f"[{a.version}] — move them into the release by hand or into fragments", file=sys.stderr)
    if a.dry_run:
        print(section)
        return
    path.write_text(insert_release(text, section), encoding="utf-8")
    for f in files:
        f.unlink()
    print(f"CHANGELOG.md: added [{a.version}] from {len(files)} fragment(s)")


if __name__ == "__main__":
    main()
