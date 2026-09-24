# Release settings: the `pypi` environment, main's review rule and the `release-tags` ruleset

Only code on `main` reaches PyPI, and `main` takes reviewed PRs only. Three workflows upload: `publish-pypi.yml`
(`lium.io`, after `release.yml` succeeds for a published release) and the two stubs `release-deprecate-lium-cli.yml`
(`lium-cli`) and `release-lium-alias.yml` (`lium`), run by hand. Each upload job runs in the **`pypi` environment**
and authenticates with PyPI **trusted publishing** (a short-lived OIDC token; no PyPI token is stored anywhere). The
settings below are repository and pypi.org settings, not files; a repository admin applies them once.

## 1. The `pypi` environment admits only `main` (admin, once)

No required reviewers: the review happens on the PR, before the code reaches `main`.

```bash
R=Datura-ai/lium
gh api -X PUT "repos/$R/environments/pypi" --input - <<'JSON'
{ "reviewers": [],
  "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }
JSON
# remove every policy other than the `main` branch (for example a `v*` tag policy), then add `main` if it is missing
gh api "repos/$R/environments/pypi/deployment-branch-policies" \
  --jq '.branch_policies[] | select(.type != "branch" or .name != "main") | .id' |
  xargs -r -I{} gh api -X DELETE "repos/$R/environments/pypi/deployment-branch-policies/{}"
gh api "repos/$R/environments/pypi/deployment-branch-policies" --jq '.branch_policies[].name' | grep -qx main ||
  gh api -X POST "repos/$R/environments/pypi/deployment-branch-policies" -f name=main -f type=branch
```

Check: `gh api "repos/$R/environments/pypi/deployment-branch-policies" --jq '.branch_policies[]|.type+" "+.name'` →
`branch main` only, and `gh api "repos/$R/environments/pypi" --jq '[.protection_rules[].type]'` → no
`required_reviewers`. The same settings are under Settings → Environments → `pypi`.

`publish-pypi.yml` starts from `workflow_run`, which always runs on `main` with `main`'s copy of the file, so it
enters the environment. It uploads only a release tag whose commit is on `main` (`git merge-base --is-ancestor`), and
it rebuilds that commit itself. The stubs run by hand, and the environment refuses a run from any branch but `main`.

## 2. pypi.org publishers (owner, on pypi.org)

For `lium.io`: Manage → Publishing → add the GitHub publisher `Datura-ai` · `lium` · workflow `publish-pypi.yml` ·
environment `pypi`, and **delete** the old `Datura-ai` · `lium` · `release.yml` publisher with no environment. Until
that one is gone, a branch with an edited `release.yml`, run by hand, can still upload. For the stubs: the same shape
with `release-deprecate-lium-cli.yml` and `release-lium-alias.yml` (a pending publisher for a name not yet on PyPI).

## 3. Main's PR rule: approval after the last push (admin, once)

Without it, an approved PR can take more commits and merge with no second review. The strictest rule of all the
rulesets on `main` applies, so turning it on in one of them is enough, for example `protect main`:

```bash
id="$(gh api "repos/$R/rulesets" --jq '.[] | select(.name == "protect main") | .id')"
gh api "repos/$R/rulesets/$id" |
  jq '{rules: [.rules[] | if .type == "pull_request" then .parameters.require_last_push_approval = true else . end]}' |
  gh api -X PUT "repos/$R/rulesets/$id" --input -
```

Check: `gh api "repos/$R/rules/branches/main" --jq '[.[]|select(.type=="pull_request")|.parameters.require_last_push_approval]'`
→ at least one `true`.

## 4. Tag ruleset

`release-tags.json` limits creating, moving and deleting `v*` tags to its bypass list (GitHub user ids,
`gh api users/<login> --jq .id`). PyPI does not depend on it: `publish-pypi.yml` refuses a tag that is not on `main`.
It still matters for the GitHub release assets: `release.yml` builds the binaries from whatever commit the tag names
and marks the release `latest`, and `install.sh` and self-update download them. Apply or update it with
`gh api "repos/$R/rulesets" --method POST --input .github/rulesets/release-tags.json` (or `PUT
"repos/$R/rulesets/<id>"` for an existing one). Check: `gh api "repos/$R/rulesets?targets=tag" --jq
'.[]|.name+" "+.enforcement'`.

## Order

(1) the environment and (3) the review rule can be set at any time; the current `main` does not upload from `pypi`.
Add the new pypi.org publisher from (2) before the merge. Merge. Right after the merge, delete the old publisher from
(2): from the merge on, `release.yml` no longer uploads, and the first release after it shows `publish-pypi.yml` works.
