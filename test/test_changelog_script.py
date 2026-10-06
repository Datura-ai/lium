"""scripts/changelog.py folds changelog.d/*.md into CHANGELOG.md the way changelog.d/README.md says."""
import importlib.util
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "changelog.py"
spec = importlib.util.spec_from_file_location("changelog_script", SCRIPT)
changelog = importlib.util.module_from_spec(spec)
spec.loader.exec_module(changelog)

RELEASED = "# Changelog\n\nintro\n\n## [0.0.37] - 2026-09-08\n\n### Added\n- old\n"


def _repo(tmp_path, changelog_text=RELEASED, fragments=None):
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d" / "README.md").write_text("# not a fragment\n", encoding="utf-8")
    for name, body in (fragments or {}).items():
        (tmp_path / "changelog.d" / name).write_text(body, encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
    return tmp_path


def test_collect_merges_same_named_sections_and_skips_the_readme(tmp_path, capsys):
    root = _repo(tmp_path, fragments={
        "DAH-1.md": "### Fixed\n- one\n",
        "DAH-2.md": "### Added\n- two\n\n### fixed\n- three\n",
        "DAH-3.md": "- loose line goes under Changed\n- second loose line\n",
        "DAH-4.md": "### Improved\n- unknown heading goes under Changed\n",
    })
    files, sections = changelog.collect(root / "changelog.d")
    assert [f.name for f in files] == ["DAH-1.md", "DAH-2.md", "DAH-3.md", "DAH-4.md"]
    assert sections == {"Added": ["- two"],
                        "Changed": ["- loose line goes under Changed", "- second loose line", "- unknown heading goes under Changed"],
                        "Fixed": ["- one", "- three"]}
    err = capsys.readouterr().err
    assert err.count("DAH-3.md: no '### Added|Changed|…' heading") == 1  # one warning per file, not per bullet
    assert "DAH-4.md: unknown section '### Improved'" in err


def test_every_fragment_in_this_repo_has_a_section_heading(capsys):
    """Of the fragments merged before this README existed, nine had no heading and were silently filed under Changed.
    This branch gives them one; a new fragment without a heading fails here on its own PR's merge ref."""
    frag_dir = SCRIPT.parent.parent / "changelog.d"
    changelog.collect(frag_dir)
    assert capsys.readouterr().err == ""


def test_render_uses_the_canonical_section_order():
    text = changelog.render("0.0.38", "2026-09-09", {"Fixed": ["- f"], "Added": ["- a"]})
    assert text.startswith("## [0.0.38] - 2026-09-09\n\n### Added\n- a\n\n### Fixed\n- f\n")


def test_release_goes_above_the_newest_released_version():
    out = changelog.insert_release(RELEASED, "## [0.0.38] - 2026-09-09\n\n### Fixed\n- f\n")
    assert out.index("## [0.0.38]") < out.index("## [0.0.37]")
    assert out.startswith("# Changelog\n\nintro\n\n")


def test_release_goes_below_an_unreleased_block_not_above_it():
    # arhangel66's review: six open PRs still add `## [Unreleased]`; the fold must not land above it.
    text = "# Changelog\n\n## [Unreleased]\n\n### Added\n- pending\n\n## [0.0.37] - 2026-09-08\n\n- old\n"
    out = changelog.insert_release(text, "## [0.0.38] - 2026-09-09\n\n### Fixed\n- f\n")
    assert out.index("## [Unreleased]") < out.index("## [0.0.38]") < out.index("## [0.0.37]")


def test_release_goes_below_unreleased_in_any_case_and_above_the_release():
    text = "# Changelog\n\n## [unreleased]\n- pending\n\n## [0.0.37] - 2026-09-08\n"
    out = changelog.insert_release(text, "## [0.0.38] - 2026-09-09\n")
    assert out.index("## [unreleased]") < out.index("## [0.0.38]") < out.index("## [0.0.37]")


def test_release_is_appended_when_there_is_no_released_version_yet():
    out = changelog.insert_release("# Changelog\n\nintro\n", "## [0.0.1] - 2026-09-09\n\n### Added\n- a\n")
    assert out.endswith("intro\n\n## [0.0.1] - 2026-09-09\n\n### Added\n- a\n\n")
    # no trailing newline on the file: the heading still starts on its own line
    out = changelog.insert_release("# Changelog\n- x", "## [0.0.1] - 2026-09-09\n")
    assert "- x\n\n## [0.0.1]" in out


def test_dry_run_prints_the_section_and_writes_nothing(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--dry-run", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert r.stdout.startswith("## [0.0.38] - 2026-09-09\n\n### Fixed\n- one\n")
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == RELEASED
    assert (root / "changelog.d" / "DAH-1.md").exists()


def test_fold_rewrites_changelog_and_deletes_only_the_fragments(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n", "DAH-2.md": "### Added\n- two\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert r.stdout.strip() == "CHANGELOG.md: added [0.0.38] from 2 fragment(s)"
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## [0.0.38] - 2026-09-09\n\n### Added\n- two\n\n### Fixed\n- one\n" in text
    assert text.index("## [0.0.38]") < text.index("## [0.0.37]")
    assert sorted(p.name for p in (root / "changelog.d").iterdir()) == ["README.md"]


def test_fold_warns_when_an_unreleased_block_is_left_out_of_the_release(tmp_path):
    text = "# Changelog\n\n## [Unreleased]\n\n### Added\n- pending\n\n## [0.0.37] - 2026-09-08\n\n- old\n"
    root = _repo(tmp_path, changelog_text=text, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert "`## [Unreleased]` block; its bullets are NOT part of [0.0.38]" in r.stderr
    out = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    assert out.index("- pending") < out.index("## [0.0.38]") < out.index("## [0.0.37]")


def test_dry_run_gives_the_same_unreleased_warning_as_the_fold(tmp_path):
    text = "# Changelog\n\n## [Unreleased]\n\n### Added\n- pending\n\n## [0.0.37] - 2026-09-08\n\n- old\n"
    root = _repo(tmp_path, changelog_text=text, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--date", "2026-09-09", "--dry-run", "--root", str(root)],
                       capture_output=True, text=True, check=True)
    assert "`## [Unreleased]` block; its bullets are NOT part of [0.0.38]" in r.stderr
    assert r.stdout.startswith("## [0.0.38] - 2026-09-09\n\n### Fixed\n- one\n")
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == text


def test_fold_refuses_a_version_that_is_already_in_the_changelog(tmp_path):
    root = _repo(tmp_path, fragments={"DAH-1.md": "### Fixed\n- one\n"})
    for extra in ([], ["--dry-run"]):  # the preview refuses the same version the fold refuses
        r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.37", "--root", str(root), *extra], capture_output=True, text=True)
        assert r.returncode != 0 and "already has a [0.0.37] section" in r.stderr
        assert r.stdout == ""
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == RELEASED
    assert (root / "changelog.d" / "DAH-1.md").exists()


def test_no_fragments_is_an_error_not_an_empty_release(tmp_path):
    root = _repo(tmp_path)
    r = subprocess.run([sys.executable, str(SCRIPT), "--version", "0.0.38", "--root", str(root)], capture_output=True, text=True)
    assert r.returncode != 0
    assert "no fragments" in r.stderr
    assert (root / "CHANGELOG.md").read_text(encoding="utf-8") == RELEASED
