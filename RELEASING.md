# Releasing lium

The version is the git tag. Publishing a GitHub release on a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which
builds the wheel and the binaries from that tag and uploads them; `publish-pypi.yml` then publishes the tag to PyPI
if it is on `main`. Nothing in the tree is bumped. Create the release with `--prerelease` so `latest` moves
only once the assets are up. The `lium` CLI and the
`lium.sdk` Python SDK ship in the same `lium.io` package, so one tag versions both.

## Choosing the version

1. Run `make next-version` on the commit you are about to tag; it prints the smallest version the changelog allows and which entries ask for it.
2. Two sources count, both read since the previous `vX.Y.Z` tag: the `changelog.d/` fragments still in the tree, and the `## [X.Y.Z]` sections that `python scripts/changelog.py --version X.Y.Z` folded into `CHANGELOG.md` (that fold deletes the fragments, so the folded section is what remains of them). `changelog.d/README.md` is documentation, not a fragment.
3. The headers decide: `### Added` (someone can do something new) needs a minor bump; `### Changed`, `### Fixed`, `### Security`, `### Deprecated` a patch. A fragment with no header, or a header not in that list, counts as `### Added` and is named in a warning — give every fragment a header.
4. `### Removed` or a bullet that starts with `BREAKING` needs a major bump once we are past 1.0, a minor bump before that.
5. No entry from either source means the next patch — unless files under `lium/`, `lium_entry.py` or `pyproject.toml` changed since the previous tag. Code with no changelog entry is refused: add a fragment to the release commit, or use the skip line below when there is nothing to tell (a dependency pin, say).
6. The release tag must be above every earlier release, whatever the changelog says: after `v0.1.0`, a `v0.0.42` is refused on any branch and nothing overrides that (the workflow marks every release `latest`). The previous release, which opens the changelog window, is the highest `vX.Y.Z` tag reachable from the release commit; only tags on the release commit itself are left out, and a second tag there is named in a warning because hatch-vcs may build it instead.
7. The release workflow refuses a tag that breaks any of this before the wheel is built — nothing is published — and names the tag to use. Delete the release *and its tag* (`gh release delete vX.Y.Z --cleanup-tag`; a refused tag left on an earlier commit would count as the previous release and hide this window), tag again, release again.
8. A lower tag is still possible for a hotfix that must ship before the unreleased features, in two steps: (a) `python3 scripts/next_version.py --defer "<why>"` writes the skipped minimum to `changelog.d/.deferred_minimum`; commit that file into the release. (b) Put `allow-version-skip: <why>` on its own line in the release notes *before you publish* (the workflow reads the notes of the publish event; editing them afterwards changes nothing — delete and publish again). The workflow logs the reason and goes on. The file is a floor for every later release until a tag reaches it, so the skipped feature cannot slip out as a patch; delete the file in the first commit after the release that satisfies it (the guard reminds you).

## The 0.1.0 catch-up

`v0.0.42` … `v0.0.46` shipped `### Added` fragments as patch releases (DAH-3328). `changelog.d/.deferred_minimum` is
seeded with `0.1.0`, so the next release must be `v0.1.0` or higher; delete the file after that release. Fold the
fragments that have piled up in `changelog.d/` in the `0.1.0` release commit (`python scripts/changelog.py --version
0.1.0`): they carry `### Added`, so whichever release first folds them is held to a minor bump — at `0.1.0` that costs
nothing, at a later `0.1.1` it would demand `0.2.0`.

## Who can publish

Only code on `main` reaches PyPI, and `main` takes reviewed PRs only. `.github/workflows/publish-pypi.yml` is the only
path to PyPI for `lium.io`. It starts after `release.yml` succeeds for a published release (`workflow_run`), so it
always runs `main`'s copy of the file: an edited copy on another branch never runs. It checks that the release tag
points at the commit the release built and that this commit is on `main`'s first-parent history (`git rev-list
--first-parent origin/main`; with squash merges only, that is a reviewed PR state), rebuilds it, and uploads from a separate job in the `pypi` environment. That environment admits only `main` and
requires one approval from a maintainer before a publish runs. Before it uploads, `publish-pypi.yml` checks that the
environment has at least one required reviewer who is not the loop's account, blocks self-approval and has admin
bypass off; the settings are in `.github/rulesets/README.md` section 1. PyPI's trusted publisher names this repository, `publish-pypi.yml` and `pypi`, so the upload
token is minted only there; there is no PyPI API token in the repository's secrets or on anyone's machine. The two
stub publishers, `release-deprecate-lium-cli.yml` (`lium-cli`) and `release-lium-alias.yml` (`lium`), run by hand in
the same environment, so they run only from `main`.

The settings behind this (the environment, the pypi.org publishers, squash merge only and approval after the last
push on `main`, and the `v*` tag ruleset) and the order to apply them are in `.github/rulesets/README.md`. The old pypi.org publisher
(`release.yml`, no environment) must be deleted right after the merge; until then a hand-run, edited `release.yml` on
a branch can still upload.

What a release looks like: create the release on a commit that is on `main` (`gh release create vX.Y.Z --prerelease
…`) → `release.yml` builds, uploads the GitHub release assets and marks the release `latest` → `publish-pypi.yml`
checks the tag, rebuilds it and uploads the wheel and sdist with PEP 740 attestations (each file's page on pypi.org
shows a *Provenance* link; `https://pypi.org/integrity/lium.io/X.Y.Z/<filename>/provenance` returns the signed
statement). A tag on a commit that is not on `main` publishes nothing to PyPI: the check fails the run.
