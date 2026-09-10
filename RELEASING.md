# Releasing lium

The version is the git tag. Publishing a GitHub release on a tag `vX.Y.Z` runs `.github/workflows/release.yml`, which
builds the wheel and the binaries from that tag, uploads them and publishes to PyPI; nothing in the tree is bumped
(DAH-3225). Create the release with `--prerelease` so `latest` moves only once the assets are up. The `lium` CLI and the
`lium.sdk` Python SDK ship in the same `lium.io` package, so one tag versions both.

## Choosing the version

1. Run `make next-version` on the commit you are about to tag; it prints the smallest version the changelog allows and which fragments ask for it.
2. The `changelog.d/` fragment headers decide: `### Added` (someone can do something new) needs a minor bump; `### Changed`, `### Fixed`, `### Security`, `### Deprecated` a patch. A fragment with no header, or a header not in that list, counts as `### Added` and is named in a warning — give every fragment a header.
3. `### Removed` or a bullet that starts with `BREAKING` needs a major bump once we are past 1.0, a minor bump before that.
4. The release workflow refuses a tag below that minimum before the wheel is built — nothing is published — and names the tag to use. Delete the release *and its tag* (`gh release delete vX.Y.Z --cleanup-tag`; a tag left behind would count as the previous release and hide this window), tag again, release again.
5. A lower tag is still possible for a hotfix that must ship before the unreleased features: put `allow-version-skip: <why>` on its own line in the release notes *before you publish* (the workflow reads the notes of the publish event; editing them afterwards changes nothing — delete and publish again); the workflow logs the reason and goes on.
