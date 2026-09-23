# Releasing lium

The version is the git tag. Publishing a GitHub release on a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which
builds the wheel and the binaries from that tag, uploads them and publishes to PyPI; nothing in the tree is bumped
(DAH-3225). Create the release with `--prerelease` so `latest` moves only once the assets are up. The `lium` CLI and the
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

Only `.github/workflows/release.yml` can upload `lium.io` to PyPI, and only from a run that the **`pypi` environment's
required reviewer approved**. Self-approval is blocked (`prevent_self_review: true`), which only refuses the account
that started the run: any other listed reviewer can approve. So the required reviewers are humans only, at least one,
and never the loop's account (`surcyf123`, user id 114649324), which never approves a release; the publish job
refuses to run while it is listed. Today (23 Sep 2026) `surcyf123` is the environment's only required reviewer, so no
release ships until an admin replaces it with a human. The environment reads `can_admins_bypass: true`: a repository
admin can deploy to `pypi` without approval; turning that off is an admin setting, and the owner decides. PyPI's
trusted publisher for the project names this repository, that file and that environment, so the upload
token is minted inside the approved job and nowhere else; there is no PyPI API token in the repository's secrets or on
anyone's machine. Two more things gate a release: the `release-tags` ruleset lets only its bypass list, humans only,
create, move or delete a `v*` tag (`gh release create` creates the tag, so it is covered), and the environment's
branch policy admits only `v*` tags and `main`. `.github/rulesets/README.md` has the admin commands and the
two checks. A human creates each release tag; the loop's account is not on the ruleset's bypass list and never
creates, moves or deletes a release tag. Today (read 23 Sep 2026) no tag ruleset is applied to this repository, so any
account with write access, the loop's account included, can create a `v*` tag until an admin applies
`.github/rulesets/release-tags.json`. The same environment covers the two stub publishers, `release-deprecate-lium-cli.yml` (`lium-cli`) and
`release-lium-alias.yml` (`lium`), which run by hand. Neither name is held on PyPI today (read 23 Sep 2026):
`https://pypi.org/pypi/lium/json` returns 404 and `https://pypi.org/simple/lium/` returns 404, so `lium` is not
registered and anyone can register it; `lium-cli` is archived with no files (`https://pypi.org/simple/lium-cli/` →
`"project-status": {"status": "archived"}`, `"files": []`). The last run of each stub failed:
`release-deprecate-lium-cli.yml` on 11 May 2026, `release-lium-alias.yml` (its only run) on 31 Mar 2026.

All of that is true once the admin steps have run **in this order**: (1) create the `pypi` environment with
self-approval blocked and humans only as required reviewers (at least one; not `surcyf123`) — before the workflow
change merges, because a missing environment is created unprotected on first use; (2) register the `pypi` publisher on pypi.org; (3) merge;
(4) proof release: a human creates the release and its tag, and a human reviewer approves it; (5) delete the old, environment-less publisher on pypi.org; (6) apply
the tag ruleset. Step (5) is the one that closes the door: until
then any account with write access can still publish through a hand-run, edited copy of the workflow. The publish
job reads the environment's rules back first and refuses to run unless there is at least one required reviewer, the
loop's account is not one of them, every reviewer is a user (it cannot read team membership), and
`prevent_self_review` is `true`; it cannot see pypi.org's publisher list.

What a release looks like after this: a human on the ruleset's bypass list runs `gh release create vX.Y.Z
--prerelease …` → the build jobs run → the run pauses at
**approve-and-publish** and GitHub e-mails the reviewer → Actions → the run → **Review deployments** → **Approve and
deploy** → the wheel and sdist go up with PEP 740 attestations (each file's page on pypi.org shows a *Provenance* link;
`https://pypi.org/integrity/lium.io/X.Y.Z/<filename>/provenance` returns the signed statement) → the GitHub release
assets job marks the release `latest`. A run nobody approves times out after 30 days and publishes nothing.
