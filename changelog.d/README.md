# Changelog fragments

One file per pull request, named after its ticket (`DAH-1234.md`; `pr-123.md` when there is no ticket). A second PR on
the same ticket adds a short suffix, `DAH-1234-<what-it-does>.md`, the way `DAH-3047-rent-by-spec.md` and
`DAH-3047-machine-rent-by-spec.md` already do; the suffix only has to make the name unique. The file holds the
`CHANGELOG.md` lines the PR would otherwise add under `## [Unreleased]`: one or more `### Added` / `### Changed` /
`### Fixed` / `### Removed` / `### Deprecated` / `### Security` headings, each followed by bullets. Example:

```markdown
### Fixed
- `lium up` no longer retries the rent POST blindly; one `lium up` could create two pods.
```

Why: every PR inserting at the same line of `CHANGELOG.md` conflicts with every other one, so no two PRs could be
merged in sequence without a rebase. Separate files cannot conflict. The fragments of PRs merged since 7 Sep 2026
already live here; `CHANGELOG.md` itself is only touched at release time.

At release time `python scripts/changelog.py --version X.Y.Z` folds every fragment into `CHANGELOG.md` under a new
`## [X.Y.Z] - YYYY-MM-DD` heading — same-named sections merged in the order Added, Changed, Deprecated, Removed, Fixed,
Security; bullets in fragment-file-name order; the fragment files deleted. The heading goes above the newest released
version and below a `## [Unreleased]` block if one exists (the script warns that the block's bullets are not part of the
release, and refuses a version that is already in the file). A fragment with no `###` heading, or an unknown one, is
filed under Changed with a warning. `--date YYYY-MM-DD` overrides today. `--dry-run` prints the section and changes
nothing; the duplicate-version refusal and the Unreleased warning run in a dry run too. Stdlib only;
`pytest test/test_changelog_script.py` covers it.
