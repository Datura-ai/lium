# e2e — this checkout's CLI and SDK against a live Lium API

The renter's first hour, asserted: `balance --json` → `ls --format json` (stable fields, `--gpu` filter) → `up`
(exactly one pod; `up` itself waits for `RUNNING` with an `ssh_cmd`, 540 s budget) → `ps` agrees → `describe --json` → `exec` (exit code 7 comes back
as exit 7 and in the JSON; `nvidia-smi -L` lists at least the GPUs billed) → `scp` up and down, byte-exact →
billing moves while the pod runs → `rm` → gone from `ps` → the final charge fits the wall clock at the node's
price (per-second, no 15-minute floor). The error contract agents depend on: wrong key exit 3 with a JSON
error, no key exit 2 naming what to do, unknown pod exit 5 for `exec`/`describe`/`rm`, `ps --format json` is a JSON list.
Then the SDK, the way `docs/developers/sdk/examples/pod-lifecycle.md` uses it: `ls` → `up` → `wait_ready` → `exec`
(success and a non-zero exit as a dict) → `upload`/`download` → `ps` → `rm`.

These are PERSONA_TESTS' renter journeys (7 Sep 2026, 31 + 16 steps by hand on staging) as tests that run on every
PR. They rent the cheapest ≥1-GPU node under `E2E_MAX_PRICE` (default $0.50/h) for a few minutes — $0.03 a run on
lium.io (7 Sep 2026: an RTX 4090 at $0.32/h for 6 min plus one at $0.35/h for 30 s), about $0.02 on staging's A4000.
Nodes in `E2E_EXCLUDE_COUNTRIES` (default `Russia,Belarus,RU,BY`; CI states the same) and executors in `E2E_EXCLUDE_EXECUTORS` (ids or huids; CI
reads the repository variable `LIUM_E2E_EXCLUDE_EXECUTORS`) are never rented: the cheapest listing is deterministic, so
a defective node — 8 Sep 2026, `brave-shark-ff` billed 2 GPUs and exposed 1 (B-119) — would fail every run until
it is excluded or fixed.

## Run it

```sh
E2E_API_KEY=<key of a funded account> ./e2e/run.sh              # staging.lium.io by default
E2E_API_URL=http://localhost:8000/api E2E_API_KEY=… ./e2e/run.sh # the lium-platform e2e stack (its seeded key)
SUITES=renter ./e2e/run.sh                                     # one journey
E2E_KEEP_POD=1 SUITES=renter ./e2e/run.sh                      # a failed step keeps the pod (no rm, no sweep; the 30-min TTL stands, or is scheduled when `up` never returned)
```

On macOS the per-step time limit needs GNU `timeout` (`brew install coreutils` provides it as `gtimeout`); without it
`run.sh` says so once and runs the steps unbounded.

`run.sh` installs this checkout (editable) plus pytest into `e2e/.venv` (uv or pip), runs `test_renter_journey.py`
and `test_sdk_journey.py` each under a hard timeout (`T_SUITE` 25m), and always leaves `e2e/artifacts/`:
`timings.txt`, `summary.md`, `<suite>-junit.xml`, `commands.json` (every CLI call with exit code, duration and the
head of its output — the key is never written anywhere). Every `lium` call runs with `HOME` set to a temp dir, so
`up` mints its SSH key there and the first-run completion hook touches no real shell rc; the key travels only as
`LIUM_API_KEY`, the target only as `LIUM_BASE_URL`.

Without `E2E_API_KEY` every test skips and `run.sh` exits 0 — nothing to run against is not a failure.

## CI (`.github/workflows/ci.yml`, job `e2e-live`)

Runs on every PR from this repo that touches `lium/**`, `e2e/**`, `pyproject.toml`, `uv.lock` or `ci.yml` (the
`e2e-inputs` job reads the PR's file list — the workflow itself has no `paths:` filter since #169, so that `ci-ok`
always reports; a README- or `test/`-only PR does not rent anything), on `workflow_dispatch`, and once a day
(`schedule`, e2e-live only — the suite and the build jobs skip on the cron) so API drift shows up without a push.
`ci-ok` (the check meant to be required; lium requires none today) does not depend on `e2e-live`: a live suite red on a platform defect (B-119) or an empty
listing must not block every merge; the sticky comment is its verdict. Needs the repository secret **`LIUM_E2E_API_KEY`** (the key of a funded account on the target API) and the
variable `LIUM_E2E_API_URL` (staging when unset). Today the variable is `https://lium.io/api` and the key belongs to
a dedicated test account funded with $200, which covers about 6,000 runs at $0.03. Fork PRs have no secrets →
the job skips and stays green. One run at a time repo-wide: two suites on one account would sweep each other's
`e2e-…` pods, and staging has a single node. The lock is the job-level `concurrency:` group `e2e-live-staging` with
`queue: max`: up to 100 jobs wait as `pending` and are served first-in-first-out by the time each started waiting
(any branch, the cron and dispatches included); that group cancels only a job past the cap, a queued job is yellow
until its turn. Before that the group ran with the default, `queue: single`, which holds ONE pending job repo-wide
and cancels it when a third PR pushes, so a push on any PR made another PR's `e2e-live` red as "cancelled" (9 Sep
2026: three PRs in one night). The group stays repo-wide on purpose — keyed per PR it would let two suites onto the
one account. The workflow-level group (per PR, `cancel-in-progress: true`) does cancel a run a newer push on the same
PR supersedes; the `e2e-…` pods of a run whose suite did not
finish its own cleanup — cancelled by the runner, killed by `run.sh`'s `timeout`, or failed under pytest-timeout — are
removed one by one by the job's cleanup step (it runs when the suite step failed or was cancelled; pytest's
finalizers do not run under those kills), the 30-min TTL being the last resort. The job and step time limits (65 / 60 min) sit above `run.sh`'s own budget (5 + 25 + 25 min), so a
double suite timeout still writes `summary.md`. Artifacts uploaded on every run;
`summary.md` posted as one sticky PR comment on `pull_request` runs (a `workflow_dispatch` run has no PR to post to).

A stale pod from a run that died mid-way (name `e2e-…`, older than 30 min) is removed at the start of the next run.

## What it cannot see

Signup and funding (a fingerprint signup creates a junk account per run; card top-up is browser + JWT only — the
lium-platform e2e stack covers signup, Stripe test mode on staging is the only place for payments), `@lium.machine`
(covered by the serverless persona; a candidate for a third journey once the decorator ships), provider commands
(`lium mine`, `lium provider …` need a chain hotkey and a host to run an executor on — the lium-io e2e covers the
executor side).
