# Release guards: the `pypi` environment and the `release-tags` ruleset

Three workflows in this repository publish to PyPI: `release.yml` (`lium.io`, on a published GitHub release of a
`vX.Y.Z` tag) and the two stubs `release-deprecate-lium-cli.yml` (`lium-cli`) and `release-lium-alias.yml` (`lium`),
run by hand. Each publish job runs in the **`pypi` environment** and authenticates with PyPI **trusted publishing**
(a short-lived OIDC token; no PyPI token is stored in this repository). The environment and the tag ruleset are
repository settings, not files; a repository admin applies them once with the commands below. The workflow files
only reference them.

## 1. The `pypi` environment (admin, once)

```bash
R=Datura-ai/lium
# create or update it with self-approval blocked; required reviewers: one or more humans, by GitHub user id
# (`gh api users/<login> --jq .id`), in place of <human-id>. Never 114649324 (surcyf123, the loop's account): the
# publish job refuses to run while it is listed. The PUT replaces the whole reviewers list. can_admins_bypass false:
# the publish job refuses to run while admin bypass is on.
gh api -X PUT "repos/$R/environments/pypi" \
  --input - <<'JSON'
{ "reviewers": [ { "type": "User", "id": <human-id> } ],
  "prevent_self_review": true,
  "can_admins_bypass": false,
  "deployment_branch_policy": { "protected_branches": false, "custom_branch_policies": true } }
JSON
# only these refs may enter the environment: release tags, and main for the two stub workflows
gh api -X POST "repos/$R/environments/pypi/deployment-branch-policies" -f name='v*'  -f type=tag
gh api -X POST "repos/$R/environments/pypi/deployment-branch-policies" -f name=main   -f type=branch
```

Check: `gh api "repos/$R/environments/pypi" --jq '.protection_rules[]|.type'` → `required_reviewers`, `branch_policy`;
`gh api "repos/$R/environments/pypi/deployment-branch-policies" --jq '.branch_policies[]|.type+" "+.name'` → `tag v*`,
`branch main`;
`gh api "repos/$R/environments/pypi" --jq '.protection_rules[]|select(.type=="required_reviewers")|.prevent_self_review'`
→ `true`; `gh api "repos/$R/environments/pypi" --jq .can_admins_bypass` → `false`;
`gh api "repos/$R/environments/pypi" --jq '.protection_rules[]|select(.type=="required_reviewers")|.reviewers[]|.type+" "+(.reviewer.id|tostring)+" "+.reviewer.login'`
→ one `User <id> <login>` line per human, and no `114649324`. A run that reaches a publish job now waits under
Actions → the run → **Review deployments** until a reviewer approves; a dispatch from any other branch is refused
before the job starts.

Self-approval is blocked (`prevent_self_review: true`): GitHub refuses an approval from the account that started the
run. That is all it blocks: any one listed required reviewer other than the run's starter can approve. So the list
holds humans only, at least one, and never 114649324 (`surcyf123`, the loop's account, which never approves a
release). List people by user id, not teams: the publish job cannot read team membership, so it refuses a team
reviewer. Today (23 Sep 2026) the `pypi` environment lists only `surcyf123`, so every publish job refuses until an
admin re-runs the `PUT` above with human ids in place of 114649324.

Admin bypass: the environment reads `can_admins_bypass: true` today. With it, a repository admin can start a
waiting publish job ("Start all waiting jobs") without a reviewer's approval. This repository has two admins today
(read 23 Sep 2026): `surcyf123`, the loop's account, and `colin-002`. So the publish job refuses to run unless
`can_admins_bypass` is `false` (a missing field counts as on), and the `PUT` above sets `"can_admins_bypass": false`.
Leaving the field out of a `PUT` does not keep today's value: the API's documented default is `true`. The same switch
is Settings → Environments → `pypi` → "Allow administrators to bypass configured protection rules".

On pypi.org the project's trusted publisher must name this environment: Manage → Publishing → Add a new publisher →
GitHub, owner `Datura-ai`, repository `lium`, workflow `release.yml` (or the stub's filename), environment `pypi`.
PyPI then accepts an upload only from a job of that file that ran inside `pypi`. The `release` environment stays: it
gates the GitHub release assets job in `release.yml`.

**Order — it matters.** (1) Create the environment with its reviewers (humans only, at least one, not 114649324),
self-approval blocked, admin bypass off and the two policies, as above, **before the workflow change merges**: a workflow that names an
environment that does not exist makes GitHub create it with no protection, and the first release would publish with
no click. The environment and both policies exist today; what step (1) still needs is the `PUT` with human
ids and `"can_admins_bypass": false`. (2) Register the
`pypi` publisher on pypi.org. (3) Merge. (4) Proof release: a human creates the release and its tag, and a human
reviewer approves the publish job. (5) **Delete the old
publisher** on pypi.org — today's `Datura-ai/lium · release.yml · (no environment)`. Until it is gone nothing fails closed: a publisher with no
environment accepts a token minted in any environment, so with no `pypi` publisher the upload goes through the old
binding, and a branch whose edited `release.yml` drops the environment, run by hand, still uploads — any of the 7
accounts with write access can do that today. (6) Apply the tag ruleset below. The publish job checks step (1)
itself: it reads `repos/$R/environments/pypi` back and stops unless the environment has at least one required
reviewer, 114649324 is not among them, every reviewer is a user, `prevent_self_review` is `true`, and `can_admins_bypass` is `false` (an
unauthenticated read for a public repository; the job holds `actions: read` for it). It cannot tell a human from
another machine account; the admin lists humans. It cannot check step (5) — pypi.org's side is the owner's click.

## 2. Tag ruleset (admin, once)

```bash
gh api "repos/$R/rulesets" --method POST --input .github/rulesets/release-tags.json
```

`release-tags.json` targets `refs/tags/v*` with the rules `creation`, `update` and `deletion`; the bypass list is the
release role, given as GitHub user ids (`gh api users/<login> --jq .id`): 4623096 (arhangel66, who has created every
release since v0.3.0). A human creates each release tag; the loop's account (114649324, `surcyf123`) is not on the
list and never creates, moves or deletes a release tag. Add a human by appending
`{ "actor_id": <human-id>, "actor_type": "User", "bypass_mode": "always" }` (never 114649324); remove one by deleting
the line and re-applying with `gh api "repos/$R/rulesets/<id>" --method PUT --input .github/rulesets/release-tags.json`.

Today (read 23 Sep 2026) no tag ruleset is applied to this repository, so any account with write access, the loop's
account included, can create a `v*` tag until an admin applies this file.

Check: `gh api "repos/$R/rulesets?targets=tag" --jq '.[]|.name+" "+.enforcement'` → `release-tags active`.
A `v*` tag pushed by anyone outside the bypass list is refused by GitHub before any workflow runs; a `gh release
create vX.Y.Z` by such a person fails the same way, because it creates the tag.

## What each guard stops

| Path an attacker with write access could take today | Stopped by |
|---|---|
| push a branch whose `release.yml` publishes without an environment, then run it with `workflow_dispatch` | the PyPI publisher bound to environment `pypi` (the token is minted only inside that environment) + the environment's branch policy (the branch cannot enter it) — **only once the old, environment-less publisher is deleted on pypi.org** (order step 5); until then this path is open |
| merge this change before the `pypi` environment exists, so GitHub creates it unprotected | the publish job's first step, which reads the environment's rules and stops when no required reviewer is set |
| the loop's account approves a release someone else started (any one listed reviewer can approve) | humans only in the reviewers list; the publish job's first step stops while 114649324 is listed or a team is |
| a repository admin (the loop's account is one today) starts a waiting publish job without approval | the publish job's first step stops unless `can_admins_bypass` is `false`; today it is `true`, and the admin `PUT` above turns it off |
| create a release on a tag of their own commit | the tag ruleset (only the bypass list, humans only, creates `v*`), then the reviewer's approval — **only once an admin applies it** (order step 6); today no tag ruleset exists |
| re-point an existing `v*` tag at another commit and re-run | the ruleset's `update` rule |
| a PyPI API token in a GitHub secret or on a laptop | none exists for these packages; trusted publishing is the only upload path |
