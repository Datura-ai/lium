# Release settings: the `pypi` environment, main's review rule and the `release-tags` ruleset

Only code on `main` reaches PyPI, and `main` takes reviewed PRs only. Three workflows upload: `publish-pypi.yml`
(`lium.io`, after `release.yml` succeeds for a published release) and the two stubs `release-deprecate-lium-cli.yml`
(`lium-cli`) and `release-lium-alias.yml` (`lium`), run by hand. Each upload job runs in the **`pypi` environment**
and authenticates with PyPI **trusted publishing** (a short-lived OIDC token; no PyPI token is stored anywhere). The
settings below are repository and pypi.org settings, not files; a repository admin applies them once.

## 1. The `pypi` environment admits only `main` and needs one maintainer approval (admin, once)

The environment deploys only from `main`, and it requires one approval from a maintainer before a publish runs: the
code review happens on the PR, and the approval confirms the upload itself. `<maintainer-id>` is the maintainer's
numeric user id (`gh api users/<login> --jq .id`); list people, not a team, and not the loop's account. Whoever starts
a run cannot approve it, and an admin cannot skip the approval. `publish-pypi.yml` checks all of this before it
uploads and publishes nothing if one setting is off.

```bash
R=Datura-ai/lium
gh api -X PUT "repos/$R/environments/pypi" --input - <<'JSON'
{ "reviewers": [ { "type": "User", "id": <maintainer-id> } ],
  "prevent_self_review": true, "can_admins_bypass": false,
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
`branch main` only, and `gh api "repos/$R/environments/pypi" --jq '[(.protection_rules[] | select(.type ==
"required_reviewers") | (.reviewers | length > 0), .prevent_self_review), .can_admins_bypass]'` → `[true,true,false]`
(at least one reviewer, self-review blocked, no admin bypass). The same settings are under Settings → Environments →
`pypi` ("Required reviewers", "Prevent self-review", "Allow administrators to bypass" off).

`publish-pypi.yml` starts from `workflow_run`, which always runs on `main` with `main`'s copy of the file, so it
enters the environment. It uploads only a release tag whose commit is on `main`'s first-parent history (`git rev-list
--first-parent origin/main`), and it rebuilds that commit itself. The stubs run by hand, and the environment refuses a run from any branch but `main`.

## 2. pypi.org publishers (owner, on pypi.org)

For `lium.io`: Manage → Publishing → add the GitHub publisher `Datura-ai` · `lium` · workflow `publish-pypi.yml` ·
environment `pypi`, and **delete** the old `Datura-ai` · `lium` · `release.yml` publisher with no environment. Until
that one is gone, a branch with an edited `release.yml`, run by hand, can still upload. For the stubs: the same shape
with `release-deprecate-lium-cli.yml` and `release-lium-alias.yml` (a pending publisher for a name not yet on PyPI).

## 3. Main's PR rules: squash merge only, approval after the last push (admin, once)

Squash merge only: a rebase merge puts each commit of a PR on `main`'s first-parent history one by one, including a
commit that a later commit of the same PR removed, so a tag on it would pass `publish-pypi.yml`'s check although the
reviewed diff never contained it. A squash merge adds one commit, the reviewed state of the PR.

```bash
gh api -X PATCH "repos/$R" -F allow_squash_merge=true -F allow_rebase_merge=false -F allow_merge_commit=false
```

Check: `gh api "repos/$R" --jq '[.allow_squash_merge, .allow_rebase_merge, .allow_merge_commit]'` →
`[true,false,false]`.

Approval after the last push:

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
`gh api users/<login> --jq .id`). It stays part of the PyPI rule permanently, even after rebase merges are off (3):
`publish-pypi.yml` accepts any commit on `main`'s first-parent history, and that history already holds commits that were
never a single reviewed PR state (rebase-merged or pushed directly). Squash merge only stops new ones, but a tag on an
existing one still passes that check, and only this ruleset stops it. It also matters for the GitHub release assets: `release.yml` builds the binaries from whatever commit the tag names
and marks the release `latest`, and `install.sh` and self-update download them. Apply or update it with
`gh api "repos/$R/rulesets" --method POST --input .github/rulesets/release-tags.json` (or `PUT
"repos/$R/rulesets/<id>"` for an existing one). Check: `gh api "repos/$R/rulesets?targets=tag" --jq
'.[]|.name+" "+.enforcement'`.

## Order

(1) the environment and (3) the merge and review rules can be set at any time; the current `main` does not upload from `pypi`.
Add the new pypi.org publisher from (2) before the merge. Merge. Right after the merge, delete the old publisher from
(2): from the merge on, only `publish-pypi.yml` uploads `lium.io`, and the first release after it shows `publish-pypi.yml` works.
