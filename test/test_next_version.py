"""scripts/next_version.py: the smallest next version from the changelog entries since the previous tag (fragments
and folded CHANGELOG.md sections), and the release.yml step that refuses a lower tag (DAH-3328).

Every case builds a real git repository with real tags and runs the script's `main` (or the script itself) against
it, the way `make next-version` and release.yml do.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "next_version.py"
FOLD_SCRIPT = REPO_ROOT / "scripts" / "changelog.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

_spec = importlib.util.spec_from_file_location("next_version", SCRIPT)
next_version = importlib.util.module_from_spec(_spec)
sys.modules["next_version"] = next_version  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(next_version)

ADDED = "### Added\n- `lium up --ready-timeout SECONDS` stops waiting.\n"
CHANGED = "### Changed\n- `lium ps` shows a `#` column.\n"
FIXED = "### Fixed\n- `--gpu 4090` finds the RTX 4090 nodes.\n"
REMOVED = "### Removed\n- `lium up --wait`; `up` always waits.\n"
BREAKING_LINE = "### Changed\n- **BREAKING**: `Lium.rent()` returns a `PodInfo`, not a dict.\n"
PROSE_BREAKING = "### Fixed\n- quotes the path without breaking the shell; a BREAKING word mid-sentence is prose.\n"
HEADERLESS = "- SDK: `Lium.ssh_session(pod)` keeps one SSH connection open for the block.\n"


class Repo:
    """A throwaway git repository: fragments are committed, tags are placed, nothing else."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "test")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "tag.gpgSign", "false")
        (root / "changelog.d").mkdir()
        (root / "changelog.d" / ".keep").write_text("")
        (root / "CHANGELOG.md").write_text("# Changelog\n\n## [0.0.41] - 2026-09-10\n\n### Fixed\n- an old fix\n")
        (root / "lium").mkdir()
        (root / "lium" / "cli.py").write_text("VERSION = 'tag'\n")
        self.commit("start")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True, text=True).stdout

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def fragment(self, name: str, text: str, message: str | None = None) -> None:
        (self.root / "changelog.d" / f"{name}.md").write_text(text)
        self.commit(message or name)

    def tag(self, name: str) -> None:
        self.git("tag", name)

    def code(self, text: str, message: str = "code") -> None:
        (self.root / "lium" / "cli.py").write_text(text)
        self.commit(message)

    def fold(self, version: str) -> str:
        """Run the repo's own scripts/changelog.py: fragments into CHANGELOG.md, fragment files deleted."""
        proc = subprocess.run(
            [sys.executable, str(FOLD_SCRIPT), "--version", version, "--date", "2026-09-14", "--root", str(self.root)],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path / "repo")


def run(repo: Repo, *args: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = next_version.main(["--repo", str(repo.root), *args])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def released(repo: Repo, tag: str, *fragments: tuple[str, str]) -> None:
    for name, text in fragments:
        repo.fragment(name, text)
    repo.tag(tag)


# ---- the bump each set of fragments asks for ---------------------------------------------------------------------


def test_only_fixes_since_the_tag_need_a_patch(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", ADDED))
    repo.fragment("DAH-2", FIXED)
    repo.fragment("DAH-3", CHANGED)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "previous tag: v0.0.41" in out
    assert "fragments since v0.0.41: 2" in out
    assert "minimum next version: 0.0.42  (patch" in out
    assert "DAH-1" not in out, "a fragment released by the previous tag is not counted again"


def test_one_feature_among_fixes_needs_a_minor(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", FIXED)
    repo.fragment("DAH-3", ADDED)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "minimum next version: 0.1.0  (minor: changelog.d/DAH-3.md has ### Added)" in out


def test_breaking_before_1_0_needs_a_minor(repo: Repo, capsys) -> None:
    released(repo, "v0.4.3", ("DAH-1", FIXED))
    repo.fragment("DAH-2", REMOVED)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert (
        "minimum next version: 0.5.0  (breaking → minor while the major is 0: changelog.d/DAH-2.md has ### Removed)"
        in out
    )


def test_breaking_after_1_0_needs_a_major(repo: Repo, capsys) -> None:
    released(repo, "v1.2.3", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.fragment("DAH-3", REMOVED)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "minimum next version: 2.0.0  (breaking: changelog.d/DAH-3.md has ### Removed)" in out


def test_a_breaking_bullet_counts_prose_does_not(repo: Repo, capsys) -> None:
    released(repo, "v1.2.3", ("DAH-1", FIXED))
    repo.fragment("DAH-2", BREAKING_LINE)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "minimum next version: 2.0.0  (breaking: changelog.d/DAH-2.md has a BREAKING bullet)" in out

    repo.tag("v2.0.0")
    repo.fragment("DAH-3", PROSE_BREAKING)
    code, out, _ = run(repo, capsys=capsys)
    assert "minimum next version: 2.0.1  (patch" in out


def test_no_fragment_since_the_tag_still_moves_the_patch(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", ADDED))
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "fragments since v0.0.41: 0" in out
    assert "minimum next version: 0.0.42" in out


def test_an_edited_or_renamed_fragment_counts(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED), ("DAH-2", FIXED + CHANGED + PROSE_BREAKING))
    repo.fragment("DAH-1", FIXED + ADDED, "DAH-1: also added a flag")
    repo.git("mv", "changelog.d/DAH-2.md", "changelog.d/DAH-2-rename.md")
    # mostly the same bytes, so git pairs old and new as a rename (R0xx) well above its 50% threshold
    (repo.root / "changelog.d" / "DAH-2-rename.md").write_text(FIXED + CHANGED + PROSE_BREAKING + REMOVED)
    repo.commit("DAH-2: renamed and edited")
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "changelog.d/DAH-1.md  minor  (### Added)" in out
    assert (
        "changelog.d/DAH-2-rename.md  breaking  (### Removed)" in out
    ), "git reports a rename+edit as R; it is a changed fragment"
    assert "minimum next version: 0.1.0" in out


def test_a_fragment_without_a_known_header_counts_as_a_feature_and_is_reported(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", HEADERLESS)
    repo.fragment("DAH-3", "### Typo\n- something\n")
    repo.fragment("DAH-4", FIXED)
    repo.fragment("DAH-5", FIXED + "### Addde\n- a new flag under a misspelt header\n")
    code, out, err = run(repo, capsys=capsys)
    assert code == 0
    assert "changelog.d/DAH-2.md  minor  (no ### header, counted as a feature)" in out
    assert (
        "changelog.d/DAH-5.md  minor  (header ### Addde is not one of" in out
    ), "a known header next to it does not hide it"
    assert (
        "changelog.d/DAH-3.md  minor  (header ### Typo is not one of added, changed, deprecated, fixed, removed, security, counted as a feature)"
        in out
    )
    assert "changelog.d/DAH-4.md  patch  (### Fixed)" in out
    assert "minimum next version: 0.1.0" in out
    assert (
        "warning: changelog.d/DAH-2.md: no ### header; counted as a feature (minor). Give it a ### Added / Changed / Fixed header."
        in err
    )
    assert "warning: changelog.d/DAH-3.md: header ### Typo is not one of" in err
    assert "warning: changelog.d/DAH-5.md: header ### Addde is not one of" in err
    assert "DAH-4" not in err


def test_since_overrides_the_previous_tag_and_quiet_prints_the_version_only(repo: Repo, capsys) -> None:
    released(repo, "v0.0.40", ("DAH-1", FIXED))
    released(repo, "v0.0.41", ("DAH-2", ADDED))
    code, out, _ = run(repo, "-q", capsys=capsys)
    assert (code, out) == (0, "0.0.42\n")
    code, out, _ = run(repo, "-q", "--since", "v0.0.40", capsys=capsys)
    assert (code, out) == (0, "0.1.0\n"), "what v0.0.41 should have been"
    with pytest.raises(SystemExit) as refused:
        run(repo, "-q", "--check", "v0.0.41", capsys=capsys)
    assert refused.value.code == 2, "-q does not combine with --check"
    code, _, err = run(repo, "--since", "v0.0.39", capsys=capsys)
    assert code == 2
    assert "v0.0.39 is not a commit in" in err


def test_tags_that_are_not_vx_y_z_are_ignored_and_no_tag_is_an_error(repo: Repo, capsys) -> None:
    code, _, err = run(repo, capsys=capsys)
    assert code == 2
    assert "no vX.Y.Z tag reachable from HEAD" in err
    repo.tag("v0.1.0-rc1")
    repo.tag("release-2026-09")
    repo.tag("v0.0.9")
    repo.fragment("DAH-1", FIXED)
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "previous tag: v0.0.9" in out
    assert "minimum next version: 0.0.10" in out


# ---- the two sources: fragments in the tree, sections folded into CHANGELOG.md ----------------------------------


def test_fragment_folded_into_changelog_and_deleted_before_the_tag_is_still_counted(repo: Repo, capsys) -> None:
    """Pixel's P1 (#245): v0.0.41, add `### Added`, fold and delete it with scripts/changelog.py, tag v0.0.42."""
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.fragment("DAH-3", FIXED)
    repo.fold("0.0.42")
    assert not (repo.root / "changelog.d" / "DAH-2.md").exists(), "the fold deletes the fragments"
    repo.commit("changelog: 0.0.42")
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert "fragments since v0.0.41: 0" in out, "git diff v0.0.41..v0.0.42 sees no fragment: added, then deleted"
    assert "CHANGELOG.md sections since v0.0.41: 1" in out
    assert "  CHANGELOG.md [0.0.42]  minor  (### Added)" in out
    assert "minimum next version: 0.1.0  (minor: CHANGELOG.md [0.0.42] has ### Added)" in out
    assert "release tag v0.0.42 is below the changelog minimum 0.1.0: CHANGELOG.md [0.0.42] has ### Added." in err

    # the section the previous tag already had is not counted again; `## [Unreleased]` is never a release
    repo.git("tag", "-d", "v0.0.42")
    repo.tag("v0.1.0")
    text = (repo.root / "CHANGELOG.md").read_text()
    (repo.root / "CHANGELOG.md").write_text(text.replace("# Changelog\n", "# Changelog\n\n## [Unreleased]\n\n### Removed\n- soon\n"))
    repo.commit("an Unreleased block")
    repo.fragment("DAH-4", FIXED)
    repo.tag("v0.1.1")
    code, out, _ = run(repo, "--check", "v0.1.1", capsys=capsys)
    assert code == 0
    assert "CHANGELOG.md sections since v0.1.0: 0" in out
    assert "minimum next version: 0.1.1  (patch" in out


def test_a_folded_section_with_a_removed_heading_counts_as_breaking(repo: Repo, capsys) -> None:
    released(repo, "v1.2.3", ("DAH-1", FIXED))
    repo.fragment("DAH-2", REMOVED)
    repo.fold("1.2.4")
    repo.commit("changelog: 1.2.4")
    code, out, _ = run(repo, capsys=capsys)
    assert code == 0
    assert "  CHANGELOG.md [1.2.4]  breaking  (### Removed)" in out
    assert "minimum next version: 2.0.0  (breaking: CHANGELOG.md [1.2.4] has ### Removed)" in out


def test_code_changed_with_no_changelog_entry_is_refused(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.code("VERSION = 'tag'\nNEW = 1\n", "a change under lium/ with no fragment")
    code, out, err = run(repo, capsys=capsys)
    assert code == 0, "without --check the report only warns"
    assert "code changed since v0.0.41 with no changelog entry: 1 file(s)\n  lium/cli.py" in out
    assert "warning: 1 file(s) under lium, lium_entry.py, pyproject.toml changed since v0.0.41 with no" in err
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert (
        "release tag v0.0.42 ships 1 changed file(s) under lium, lium_entry.py, pyproject.toml since v0.0.41 but no "
        "changelog.d fragment and no CHANGELOG.md section describes them (lium/cli.py)." in err
    )
    code, _, err = run(repo, "--check", "v0.0.42", "--allow-skip", "dependency pin only", capsys=capsys)
    assert code == 0, "the skip line covers a release with nothing to tell; no minimum is deferred by it"
    assert "warning: release tag v0.0.42 ships code with no changelog entry; allowed by allow-version-skip: dependency pin only" in err

    # with a deferred floor in the tree the missing entry is still the first thing named, not the floor
    (repo.root / "changelog.d" / ".deferred_minimum").write_text("0.1.0\n# earlier skip\n")
    repo.commit("deferred 0.1.0")
    repo.git("tag", "-d", "v0.0.42")
    repo.tag("v0.0.42")
    code, _, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert "ships 1 changed file(s)" in err
    assert "below the changelog minimum" not in err
    repo.git("reset", "-q", "--hard", "HEAD~1")
    repo.git("tag", "-d", "v0.0.42")

    # a fragment describes it → fine; a change outside the code paths never needs one
    repo.fragment("DAH-2", FIXED)
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert (code, err) == (0, "")
    assert "code changed since" not in out
    (repo.root / "README.md").write_text("docs only\n")
    repo.commit("docs")
    repo.tag("v0.0.43")
    code, out, err = run(repo, "--check", "v0.0.43", capsys=capsys)
    assert (code, err) == (0, "")
    assert "fragments since v0.0.42: 0" in out


# ---- --check: what the release workflow runs -----------------------------------------------------------------------


def test_v0_1_0_then_v0_0_42_is_refused_as_going_backwards(repo: Repo, capsys) -> None:
    """Pixel's P1 (#245): tag a patch-only commit v0.1.0, add a fix, tag it v0.0.42 — the guard must refuse."""
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", FIXED)
    repo.tag("v0.1.0")
    code, out, err = run(repo, "--check", "v0.1.0", capsys=capsys)
    assert (code, err) == (0, "")
    assert "release tag v0.1.0 is at or above the changelog minimum 0.0.42: ok" in out, "a bigger jump is fine"
    repo.fragment("DAH-3", FIXED)
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert "previous tag: v0.1.0" in out, "the previous release is chosen without looking at the candidate"
    assert (
        "release tag v0.0.42 is not above the previous release v0.1.0: a release never goes backwards (the workflow "
        "marks every release latest). Delete the release AND its tag (`gh release delete v0.0.42 --cleanup-tag`) and tag "
        "v0.1.1 or higher." in err
    )
    code, _, err = run(repo, "--check", "v0.0.42", "--allow-skip", "hotfix", capsys=capsys)
    assert code == 1, "no skip line makes a release go backwards"
    assert "is not above the previous release v0.1.0" in err
    repo.git("tag", "-d", "v0.0.42")
    repo.tag("v0.1.0-again")  # not vX.Y.Z: ignored
    repo.tag("v0.1.1")
    code, out, err = run(repo, "--check", "v0.1.1", capsys=capsys)
    assert (code, err) == (0, "")
    assert "previous tag: v0.1.0" in out


def test_a_tag_below_a_release_on_another_branch_is_refused_too(repo: Repo, capsys) -> None:
    """release.yml marks every release `latest`: a v0.0.47 cut from v0.0.46 while v0.1.0 exists moves latest back."""
    released(repo, "v0.0.46", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.1.0")
    repo.git("checkout", "-q", "-b", "hotfix-0.0.x", "v0.0.46")
    repo.fragment("DAH-3", FIXED)
    repo.tag("v0.0.47")
    code, out, err = run(repo, "--check", "v0.0.47", capsys=capsys)
    assert code == 1
    assert "previous tag: v0.0.46" in out, "v0.1.0 is not reachable from the hotfix branch"
    assert "minimum next version: 0.0.47" in out, "the changelog window itself is fine"
    assert (
        "release tag v0.0.47 is not above the latest release v0.1.0: a release never goes backwards (the workflow marks "
        "every release latest). Delete the release AND its tag (`gh release delete v0.0.47 --cleanup-tag`) and tag "
        "v0.1.1 or higher." in err
    )




def test_check_refuses_a_tag_below_the_minimum_and_names_the_tag_to_use(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert (
        "previous tag: v0.0.41" in out
    ), "the previous tag is the highest one below the checked tag, not the checked tag"
    assert "release tag v0.0.42 is below the changelog minimum 0.1.0: changelog.d/DAH-2.md has ### Added." in err
    assert "then either tag v0.1.0 and release that, or commit changelog.d/.deferred_minimum" in err
    assert "publish the release again with a line `allow-version-skip:" in err


def test_check_passes_a_tag_at_or_above_the_minimum(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.1.0")
    code, out, err = run(repo, "--check", "v0.1.0", capsys=capsys)
    assert (code, err) == (0, "")
    assert "release tag v0.1.0 is at or above the changelog minimum 0.1.0: ok" in out

    repo.fragment("DAH-3", FIXED)
    repo.tag("v1.0.0")
    code, out, _ = run(repo, "--check", "v1.0.0", capsys=capsys)
    assert code == 0
    assert "previous tag: v0.1.0" in out
    assert (
        "release tag v1.0.0 is at or above the changelog minimum 0.1.1: ok" in out
    ), "a bigger jump than needed is fine"


def test_skip_on_v0_0_42_holds_v0_0_43_to_the_deferred_minimum(repo: Repo, tmp_path: Path, capsys) -> None:
    """Pixel's P2 (#245): a skipped `### Added` on v0.0.42 must still force v0.0.43 up to 0.1.0."""
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.fragment("DAH-3", FIXED)
    repo.tag("v0.0.42")
    notes = tmp_path / "notes.md"
    notes.write_text(
        "## What's Changed\r\n* DAH-3 hotfix\r\n\r\nAllow-Version-Skip: ssh pin hotfix; the DAH-2 feature waits for 0.1.0\r\n"
    )
    code, _, err = run(repo, "--check", "v0.0.42", "--release-notes", str(notes), capsys=capsys)
    assert code == 1, "the skip line alone is not enough: nothing would remember the skipped minimum"
    assert (
        "allow-version-skip needs that minimum persisted, but changelog.d/.deferred_minimum at v0.0.42 is not in the tree"
        in err
    )

    assert "Delete the release AND its tag (`gh release delete v0.0.42 --cleanup-tag`), run `python3" in err

    # the tagger does what the message says: deletes the tag, persists the skipped minimum, commits, tags again
    repo.git("tag", "-d", "v0.0.42")
    code, out, _ = run(repo, "--defer", "ssh pin hotfix first", capsys=capsys)
    assert code == 0
    assert "minimum next version: 0.1.0  (minor: changelog.d/DAH-2.md has ### Added)" in out
    assert "wrote 0.1.0 to" in out
    assert (repo.root / "changelog.d" / ".deferred_minimum").read_text() == "0.1.0\n# ssh pin hotfix first\n"
    repo.commit("defer 0.1.0")
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", "--release-notes", str(notes), capsys=capsys)
    assert code == 0
    assert "deferred minimum: 0.1.0  (changelog.d/.deferred_minimum: ssh pin hotfix first)" in out
    assert (
        "warning: release tag v0.0.42 is below the changelog minimum 0.1.0; allowed by allow-version-skip: ssh pin hotfix; "
        "the DAH-2 feature waits for 0.1.0; changelog.d/.deferred_minimum holds the next release to 0.1.0" in err
    )

    # the next patch-only release: the DAH-2 fragment is outside the new window, the deferred file is not
    repo.fragment("DAH-4", FIXED)
    repo.tag("v0.0.43")
    code, out, err = run(repo, "--check", "v0.0.43", capsys=capsys)
    assert code == 1
    assert "previous tag: v0.0.42" in out
    assert "fragments since v0.0.42: 1" in out
    assert (
        "minimum next version: 0.1.0  (deferred minimum: changelog.d/.deferred_minimum says 0.1.0: ssh pin hotfix first)"
        in out
    )
    assert "release tag v0.0.43 is below the changelog minimum 0.1.0: changelog.d/.deferred_minimum says 0.1.0" in err
    code, _, err = run(repo, "--check", "v0.0.43", "--allow-skip", "again", capsys=capsys)
    assert code == 0, "a second skip is allowed while the file already holds 0.1.0"
    assert "holds the next release to 0.1.0" in err

    # 0.1.0 satisfies it; the guard says to delete the file, and a file left behind is only a warning
    repo.tag("v0.1.0")
    code, out, err = run(repo, "--check", "v0.1.0", capsys=capsys)
    assert code == 0
    assert "release tag v0.1.0 is at or above the changelog minimum 0.1.0: ok" in out
    assert "warning: changelog.d/.deferred_minimum (0.1.0) is satisfied by v0.1.0; delete the file" in err
    repo.fragment("DAH-5", FIXED)
    repo.tag("v0.1.1")
    code, out, err = run(repo, "--check", "v0.1.1", capsys=capsys)
    assert code == 0
    assert "minimum next version: 0.1.1  (patch" in out
    assert "warning: changelog.d/.deferred_minimum says 0.1.0, already released by v0.1.0; delete the file." in err


def test_allow_version_skip_needs_a_reason(repo: Repo, tmp_path: Path, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.0.42")
    notes = tmp_path / "notes.md"
    notes.write_text("## What's Changed\n* nothing about a skip\n")
    code, _, err = run(repo, "--check", "v0.0.42", "--release-notes", str(notes), capsys=capsys)
    assert code == 1, "notes without the line do not skip"
    notes.write_text("allow-version-skip:\n")
    code, _, err = run(repo, "--check", "v0.0.42", "--release-notes", str(notes), capsys=capsys)
    assert code == 2
    assert "allow-version-skip needs a reason" in err
    code, _, err = run(repo, "--check", "v0.0.42", "--allow-skip", "  ", capsys=capsys)
    assert code == 2, "--allow-skip with a blank reason is refused too"
    with pytest.raises(SystemExit) as refused:
        run(repo, "--defer", " ", capsys=capsys)
    assert refused.value.code == 2, "--defer needs a reason too"


def test_a_deferred_minimum_file_that_is_not_x_y_z_is_an_error(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    (repo.root / "changelog.d" / ".deferred_minimum").write_text("v0.1.0\n")
    repo.commit("bad deferred file")
    with pytest.raises(SystemExit) as refused:
        run(repo, capsys=capsys)
    assert refused.value.code == 2
    assert "changelog.d/.deferred_minimum at HEAD: first line 'v0.1.0' is not X.Y.Z" in capsys.readouterr().err


def test_check_refuses_a_tag_that_is_not_vx_y_z_or_that_is_not_here(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    for bad in ("0.0.42", "0.0.42x", "v0.1", "v0.1.0-rc1"):
        code, _, err = run(repo, "--check", bad, capsys=capsys)
        assert code == 2, bad
        assert f"release tag {bad!r} is not vX.Y.Z; install.sh and the binary's startup self-update fetch" in err
    code, _, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 2
    assert "v0.0.42 is not a commit in" in err


def test_check_ignores_a_refused_tag_left_on_the_release_commit(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.0.42")  # refused by the guard, but the tagger did not delete it
    repo.tag("v0.1.0")  # the tag that replaced it, on the same commit
    code, out, err = run(repo, "--check", "v0.1.0", capsys=capsys)
    assert code == 0
    assert (
        "previous tag: v0.0.41" in out
    ), "a tag on the release commit released nothing; v0.0.42 is not the previous tag"
    assert (
        "warning: tag v0.0.42 also points at the release commit; hatch-vcs may build 0.0.42 instead of v0.1.0 — "
        "`gh release delete v0.0.42 --cleanup-tag` first." in err
    ), "the build one step later would ship 0.0.42; the guard names the leftover tag"
    assert "minimum next version: 0.1.0" in out
    code, out, _ = run(repo, capsys=capsys)
    assert "previous tag: v0.1.0" in out, "without --check the highest tag on HEAD is the previous tag"

    code, _, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 1
    assert "Delete the release AND its tag (`gh release delete v0.0.42 --cleanup-tag`" in err


def test_check_reads_the_fragments_at_the_tag_not_at_head_and_only_md_files(repo: Repo, capsys) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", FIXED)
    (repo.root / "changelog.d" / "notes.txt").write_text("### Added\n- not a fragment: not .md\n")
    repo.commit("changelog.d/notes.txt")
    repo.tag("v0.0.42")
    repo.fragment("DAH-3", ADDED, "a feature merged after the tag")
    code, out, _ = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 0
    assert "fragments since v0.0.41: 1" in out, "changelog.d/notes.txt is not a fragment"
    assert "DAH-3" not in out


def test_changelog_d_readme_md_is_not_a_fragment(repo: Repo, capsys) -> None:
    """Pixel's P2 (#245): the real file is changelog.d/README.md, and its examples may carry ### Added."""
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    (repo.root / "changelog.d" / "README.md").write_text(
        "# Changelog fragments\n\nExample:\n\n```markdown\n### Added\n- `lium up --ttl`\n### Removed\n- nothing\n```\n"
    )
    repo.commit("changelog.d/README.md")
    repo.fragment("DAH-2", FIXED)
    repo.tag("v0.0.42")
    code, out, err = run(repo, "--check", "v0.0.42", capsys=capsys)
    assert code == 0
    assert "fragments since v0.0.41: 1" in out
    assert "README" not in out and "README" not in err
    assert "minimum next version: 0.0.42  (patch" in out


def test_the_script_runs_as_a_subprocess_the_way_the_workflow_calls_it(repo: Repo) -> None:
    released(repo, "v0.0.41", ("DAH-1", FIXED))
    repo.fragment("DAH-2", ADDED)
    repo.tag("v0.0.42")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo.root), "--check", "v0.0.42"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "minimum next version: 0.1.0" in proc.stdout
    assert "tag v0.1.0 and release that" in proc.stderr
    assert "::error::" not in proc.stdout, "annotations only under GitHub Actions"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--repo", str(repo.root), "--check", "0.0.42"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GITHUB_ACTIONS": "true"},
    )
    assert proc.returncode == 2
    assert "::error::release tag '0.0.42' is not vX.Y.Z" in proc.stdout, "an exit-2 refusal is annotated like an exit-1"


# ---- release.yml wires the check in before anything is built -----------------------------------------------------


def test_release_workflow_checks_the_tag_before_the_build() -> None:
    workflow = yaml.safe_load(RELEASE_WORKFLOW.read_text())
    steps = workflow["jobs"]["build-python"]["steps"]
    names = [step.get("name") for step in steps]
    check = steps[names.index("Release tag must be ≥ the changelog minimum")]
    assert names.index("Release tag must be ≥ the changelog minimum") < names.index("Build package")
    assert check["if"] == "github.event_name == 'release'"
    assert check["env"] == {
        "RELEASE_BODY": "${{ github.event.release.body }}"
    }, "the notes go through env, never into the script text"
    assert 'python3 scripts/next_version.py --check "$RELEASE_TAG" --release-notes' in check["run"]
    for publishing_job in ("release-assets", "approve-and-publish"):
        needs = workflow["jobs"][publishing_job]["needs"]
        needs = [needs] if isinstance(needs, str) else needs
        assert "build-python" in needs, f"{publishing_job} publishes only after the check"
