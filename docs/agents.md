# Lium for agents and scripts

One page for an LLM agent (or any unattended script) that has to rent a GPU pod, run work on it, collect the results and give the pod back, without a human at the keyboard. Everything here is a shell command plus `jq`, or the Python SDK; nothing needs a terminal. Every `lium …` line on this page, and the list of `--json` commands in §2, is resolved against this version's command tree by `test/test_agent_docs.py`; check `lium <command> --help` for anything not covered here.

The rules of the road:

1. **Authenticate from the environment.** `LIUM_API_KEY` wins over `~/.lium/config.ini`. Never run `lium init` from an agent.
2. **Ask for JSON.** `--format json` on `ls`/`ps`, `--json` on `exec`/`describe`/`balance`: the result is on stdout and the exit code says whether it worked. On any of them a runtime error is one JSON object on stderr (`LIUM_OUTPUT=json` switches that on for every renter command); `lium provider` prints its error envelope on stdout under `--json` or `LIUM_OUTPUT=json` (§7). A usage error is click's plain text with exit 2 (§2).
3. **Never let a command wait for a human.** Pass `--yes` to anything that would confirm (`up`, `rm`); `reboot` never asks.
4. **Always give a pod a lifetime** (`--ttl`) and always remove it when finished, including on failure.

## 1. Authentication

```bash
export LIUM_API_KEY="..."          # from the Lium dashboard; takes precedence over the config file
lium balance --json                # cheapest possible "am I authenticated" check
```

SSH: the CLI and SDK use `LIUM_SSH_KEY_PATH` if set, else `[ssh] key_path` in `~/.lium/config.ini`, else the first of `~/.ssh/id_ed25519`, `~/.ssh/id_rsa`, `~/.ssh/id_ecdsa` that exists (`lium whoami` prints the one in use); `lium up` registers its public key on the account on first use. Generate one first on a fresh machine:

```bash
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -q -t ed25519 -N '' -f ~/.ssh/id_ed25519
```

## 2. Reading results and errors

Success: the JSON result is on **stdout**, exit code 0.

Failure on a renter command that takes `--json` (`up`, `exec`, `describe`, `balance`, `whoami`, `audit`, `cp`, `init`; `fund`, `signup`, `topup currencies`, `topup create`, `topup card`, `keys create`, `keys list`, `keys show`, `keys scopes`, `keys budget`, `workspaces list` and `workspaces members` too): stdout is empty, **stderr** holds one JSON object, the exit code is non-zero:

```json
{"ok": false, "error": {"code": "pod_not_found", "message": "No pods match targets: train-1", "hint": "Run 'lium ps' to list pods; a name, huid, id or 1-based index is accepted", "exit_code": 5}}
```

All four keys are always there: `error.hint` is the next command or option to try, `error.exit_code` repeats the process exit code. The codes and hints are listed in [docs/exit-codes.md](exit-codes.md).

A usage error on any command — an unknown option, a missing argument — is click's plain-text `Usage: … Error: No such option '--bogus'` on stderr with exit 2, not the envelope: read exit 2 with non-JSON stderr as "fix the invocation".

Failure on `ls --format json` / `ps --format json`: the same envelope on **stderr**, stdout empty — `lium ps no-such-pod --format json` exits 5 with `{"ok": false, "error": {"code": "pod_not_found", …}}` on stderr. `LIUM_OUTPUT=json` in the environment turns the envelope on for every renter command's failures, flag or no flag; success output stays JSON only where `--format json`/`--json` asks for it. `lium provider` is the exception: there `LIUM_OUTPUT=json` acts like `--json`, success output is JSON too and the error envelope is on stdout (§7).

Exit codes:

| Code | Meaning | Typical reaction |
|---|---|---|
| 0 | success | continue |
| 1 | unclassified failure | inspect `error.message`, usually retry once |
| 2 | bad arguments or configuration | fix the invocation; do not retry as-is |
| 3 | the API refused or failed | back off and retry; give up after a few attempts |
| 4 | SSH could not connect | pod still booting: wait and retry |
| 5 | pod not found | re-list with `lium ps` |
| 6 | permission denied | stop; `lium balance --json` |

Pattern in a script:

```bash
set -o pipefail
if ! out=$(lium exec "$POD" --json "nvidia-smi -L" 2>err.json); then
  if [ -n "$out" ]; then
    # the remote command failed: the envelope is on stdout, err.json is empty
    jq -r '.results[] | "\(.pod): exit \(.exit_code): \(.stderr)"' <<<"$out"
  else
    # the CLI itself failed (no such pod, the API refused): stdout is empty, the error is in err.json
    code=$(jq -r .error.code err.json)
  fi
  ...
fi
```

`lium exec --json` exits non-zero in two ways. When the remote command fails, the exit code is the remote one and the
`{"ok": false, "results": […]}` envelope is on **stdout** (`results[]` carries the remote `exit_code`, `stdout` and
`stderr`); stderr is empty. When the CLI could not run it at all — pod not found, the API refused — stdout is empty and the
`{"ok": false, "error": …}` envelope is on **stderr**. Check `$out` first.

## 3. The lifecycle, end to end

### Pick a node

```bash
lium ls --gpu H100 --count 1 --format json | jq -r '.[0].huid'
```

`ls` output is sorted cheapest $/GPU·h first, unpriced nodes last (the same order as the table; `--sort` picks another key), so `.[0]` is the cheapest matching node and `is_pareto: true` marks the nodes the table stars; each entry carries `huid`, `gpu_type`, `gpu_count`, `price_per_hour`, `vram_gb`, `disk_gb`, `max_cuda_version`, `country`. Index numbers (`1`, `2`) refer to the *last* `lium ls` run in this shell's config directory; a fresh agent should use the `huid`.

### Rent it

`lium up --json` prints the ready pod as one JSON document on stdout, `{"pod": {…}}` with the keys of one `lium ps --format json` row (plus `termination_time` when `--ttl`, `--until` or `--budget` set one), and returns instead of opening a shell; progress lines go to stderr:

```bash
POD=$(lium up "$NODE" --ttl 4h --yes --json | jq -r '.pod.huid')
```

`--yes` skips the price confirmation (behind a pipe it cannot be asked, and `up` fails with `confirmation_required` before renting), `--json` implies `--no-ssh`, `--ttl` is the safety net. `up` waits until SSH is reachable before printing. When `up` fails after the rent landed (`pod_not_ready`, `api_timeout`, …), the envelope's `data.pod_id` / `data.pod_name` name the pod that bills: `lium rm` it, do not run `up` again. Check the GPU count yourself before spending on the job:

```bash
lium exec "$POD" --json "nvidia-smi -L | wc -l"    # must equal gpu_count from `lium ps --format json`
```

In Python, remove the pod in a `finally:` so it goes even when your code raises:

```python
from lium.sdk import Lium
lium = Lium()
created = lium.up(executor_id=node_id, name=name)
try:
    pod = lium.wait_ready(created, timeout=600)          # PodInfo once RUNNING with SSH details
    print(lium.exec(pod, command="nvidia-smi -L")["stdout"])
finally:
    for p in lium.ps():                                   # remove it even if wait_ready timed out
        if p.id == created["id"]:
            lium.down(p)
```

### Run work

Foreground, with the output back as JSON:

```bash
lium exec "$POD" --json "python -c 'import torch; print(torch.cuda.device_count())'"
```

Background, so a long training run survives the end of the SSH session — `exec` blocks until the remote command exits and prints its output then, so start the job detached and poll its log:

```bash
PID=$(lium exec "$POD" --json "mkdir -p /root/logs; nohup setsid bash -lc 'cd /root/project && python train.py' > /root/logs/train.log 2>&1 < /dev/null & echo \$!" | jq -r '.results[0].stdout' | tr -d '[:space:]')
```

The `setsid`, the `< /dev/null` and the redirection all matter: without them the process is tied to the SSH session and dies when `exec` returns. The `;` after `mkdir` matters too: with `&&` the `&` would background the whole `mkdir … && nohup …` list as a subshell, and `$!` would be that subshell's PID, not the job's. Capture through `--json`: without it `exec` prints `Executing on <pod>` on stdout before the remote output, so a plain capture holds two lines, not a PID; the JSON envelope's `.results[0].stdout` is the remote stdout alone.

Poll it (`tail -f` would never return — `exec` reads the output after the command exits):

```bash
lium exec "$POD" --json "kill -0 $PID && echo running || echo done; tail -n 20 /root/logs/train.log"
```

### Watch the GPUs

```bash
lium exec "$POD" --json "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw --format=csv,noheader,nounits"
```

A pod that shows 0 % utilisation for several minutes after the job started is usually loading data from the wrong place (see gotchas) or has crashed: read the log before deciding.

### Move data

```bash
lium rsync "$POD" ./data /root/data                              # mirrors rsync -avz
lium scp "$POD" /root/out/model.safetensors ./out -d             # one file per call; -d/--download pulls from the pod
lium exec "$POD" --json "tar czf /root/out.tgz -C /root out" >/dev/null && lium scp "$POD" /root/out.tgz . -d   # a directory: pack it first
```

`lium rsync` uploads only (`TARGETS LOCAL_PATH [REMOTE_PATH]`) and installs `rsync` on the pod when it is missing (`apt-get install -y rsync`, so the first run on a minimal image takes longer). A failed `rsync` is simply re-run. `lium scp -d` downloads one file per call — a results directory is packed on the pod first, as above.

### Remove the pod

```bash
lium rm "$POD" --yes
```

Do this in a trap so it runs on any exit path:

```bash
trap 'lium rm "$POD" --yes >/dev/null 2>&1 || true' EXIT
```

`lium rm --all --yes` removes every pod on the account; do not use it from a shared account.

## 4. Pod gotchas

- **Know which disk persists.** On the standard templates the pod's volume is mounted at `/root` — that is what `lium bk` backs up and what encryption covers (only an attached Volume, `--volume`, outlives `lium rm`); `/workspace` and `/tmp` are plain container filesystem and are gone with the pod. Keep the project, the venv, weights, datasets, checkpoints and the Hugging Face cache on the volume (the same rule as the README and "First hour" pages); use `/workspace` only for scratch you can re-download.
- **Point Hugging Face at the volume before the first download:** `mkdir -p /root/hf && export HF_HOME=/root/hf` (and `HF_HUB_ENABLE_HF_TRANSFER=1` after `pip install hf_transfer`).
- **PEP 668 on Ubuntu 24.04 images:** a bare `pip install` fails with "externally managed environment". Use `python -m venv /root/venv && . /root/venv/bin/activate`, or `export PIP_BREAK_SYSTEM_PACKAGES=1`.
- **Blackwell needs a recent PyTorch build.** B200, B300, RTX PRO 6000 and RTX 5090 are not supported by wheels built for CUDA ≤ 12.4; install a cu128 or cu130 wheel (`pip install torch --index-url https://download.pytorch.org/whl/cu130`). A template built on cu126 on a Blackwell executor produces "no kernel image is available" at the first CUDA call.
- **FlashAttention-3 is Hopper-only** (H100/H200). On Blackwell use FlashAttention-4 or PyTorch's SDPA (`torch.nn.attention.sdpa_kernel`).
- **Background processes must be detached:** `nohup setsid <cmd> > log 2>&1 < /dev/null &`. A plain `nohup cmd &` through `lium exec` can die with the session.
- **Minimal images lack tools** scripts assume: `apt-get update && apt-get install -y rsync ffmpeg git`.
- **Set `--ttl` on every `up`.** Billing runs until the pod is removed, whether or not the job is alive.
- **Executor vs. template CUDA:** `lium ls --format json` reports `max_cuda_version` per node; choose a template whose CUDA build does not exceed it.

## 5. Discovering the shapes

`lium ps --format json` / `lium ls --format json` return the table-equivalent objects shown above, and `lium describe <pod> --json` returns the full pod manifest. The other `--json` commands of §2:

- `lium cp <src-pod>:<path> <dst-pod>:<path> --json` returns one object: `ok`, `source` and `destination` (each `{"pod": <huid>, "path": …}`) and rsync's `exit_code`.
- `lium init --api-key <key> --json` returns one object: `ok`, `api_key_source` (the same value `whoami --json` prints), `saved_from`, `env_key`, `active_workspace`, `config_path`, `ssh_key_path`; `--json` needs `--api-key` or an exported `LIUM_API_KEY`, the browser flows print for a person (rule 1: export the key instead when you can).
- `lium keys create <name> --json` returns the new key as the server sends it (the secret under `key`, printed this once) plus `workspace_name`; needs `lium workspaces login` first. Without `--scope` the key gets `read`, `rent` and `manage` — never `billing`, the money-moving scope, which is added only by `--scope billing` (a warning line goes to stderr once the key is minted, so a refused create leaves only the error envelope there). `billing` stands alone: `--scope billing` with `read`, `rent` or `manage` is `invalid_arguments` (exit 2) before any request — refused by the CLI itself. `--daily-budget USD` / `--monthly-budget USD` / `--max-budget USD` cap what the key's pods may be billed per UTC day / per UTC month / over the key's lifetime, `--pod-visibility own|account` says whether the key sees only the pods it rented or every pod of the account (not passed: the field is not sent and the server's default decides). Budgets are at least $1 in whole cents and keep daily ≤ monthly ≤ max (exit 2 otherwise). `--scope billing` and its money-route rule (card payments, credit transfers and crypto payments through the API need a key holding `billing`), budgets and visibility need a server with per-key budgets (not on lium.io yet: its `POST /keys` takes `read`, `rent` and `manage` only, so `--scope billing` is refused there). On an older server a `create` that asks for a budget or visibility is `invalid_arguments` (exit 2) — a budget before minting when the server has no `GET /keys/scopes`, otherwise after it (visibility is judged on the echo alone), with the uncapped key revoked and `data.unrecorded` naming the fields — unless `--allow-unbudgeted` is passed, which keeps the key and puts a warning on stderr.
- `lium keys list --json` returns a list of the workspace's key rows (`id`, `name`, `scopes`, `created_at`, `last_used`, and — `null` from a server without per-key budgets — `daily_budget_usd`, `monthly_budget_usd`, `max_budget_usd`, `spent_today_usd`, `spent_month_usd`, `spent_total_usd`, `pod_visibility`, `pods_count`) without the key material; needs `lium workspaces login` first.
- `lium keys show <name|id> --json` returns one key's row (with `pods_count`, the active pods the key created) plus `can_do`: one `<scope>: <line>` per line of the server's `can` list for each scope the key holds (`GET /keys/scopes`; the scope name alone on an older server), `refusals`: the server's `GET /keys/{id}/refusals` rows newest first (each names the `window` hit, the `route`, the `amount_usd` asked; `null` from a server without the ledger) and `refusals_today`, how many fell on the current UTC day. `lium keys scopes --json` returns the server's body whole: `scopes` as `{"scope", "title", "description", "can", "route_families", "default"}`, `pod_visibility` as `{"value", "description", "default"}`, and `money_routes` (needs a newer Lium server, not on lium.io yet; older servers answer `not_found`). `lium keys budget <name|id> --json` (`--daily-budget USD`, `--monthly-budget USD`, `--max-budget USD`, `--no-daily-budget`, `--no-monthly-budget`, `--no-max-budget`) returns the key's row after `PATCH /keys/{id}`; naming nothing, setting and clearing the same budget, or a wider window below a narrower one, is `invalid_arguments` (exit 2) before any request; a window not named keeps the key's current budget and counts in that order too (`--monthly-budget 5` on a key with a $20 daily budget is refused after the key is read, before the `PATCH`); a server without the route is `not_found` (exit 3), a window it did not record `invalid_arguments` (exit 2, `data.unrecorded`). `lium ps --key <name|id> --format json` and `lium billing history --key <name|id> --format json` keep the rows stamped with that `api_key_id`; on a server that stamps none the lists are the whole account's and one stderr line says the server cannot filter by key — the statement's body says it too, `"api_key_filter": {"api_key_id": "…", "applied": false}`.
- `lium ps --key <name|id> --format json` lists only the pods rented through that key (each row then carries `api_key_id` / `api_key_name`); `lium billing history [--key <name|id>] [--from YYYY-MM-DD] [--to YYYY-MM-DD] --format json` prints the ledger statement, `{"start_day", "end_day", "total", "pods": [...]}`, each pod with its `total`, `billed_seconds` and per-day `days`; with `--key` also `api_key_filter` (`{"api_key_id", "applied"}` — `applied: false` means the server could not filter and the figures are the whole account's). A key name needs `lium workspaces login`; an id does not. A request refused by the key's budget — a rent (`up`), a pod's schedule change (`rm --in/--at`), a top-up (`fund`, `topup create`, `topup card`) — fails alike with the server's code `API_KEY_BUDGET_EXCEEDED` (exit 6; the message names the window hit — daily, monthly or lifetime; `data.window` is `daily` / `monthly` / `max`, with `data.budget_usd`, `data.spent_usd`, `data.api_key_id`), a route the key's scopes do not cover with `missing_scope` (exit 6, `data.scope`).
- `lium workspaces list --json` returns a list of `{"id", "name", "role", "billing_owner_user_id", "pending_billing_owner_user_id", "is_personal", "created_at"}`: the key's own workspace, or every one you belong to with a session.
- `lium workspaces members [<workspace>] --json` returns a list of `{"user_id", "name", "email", "role", "is_billing_owner", "joined_at"}`.

## 6. A complete run

```bash
#!/usr/bin/env bash
set -euo pipefail

NODE=$(lium ls --gpu H100 --count 1 --format json | jq -r '.[0].huid')
POD=$(lium up "$NODE" --ttl 4h --yes --json | jq -r '.pod.huid')
trap 'lium rm "$POD" --yes >/dev/null 2>&1 || true' EXIT

lium rsync "$POD" ./project /root/project
lium exec "$POD" --json "cd /root/project && python -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt" >/dev/null
lium exec "$POD" --json "cd /root/project && . .venv/bin/activate && python train.py --epochs 1"
lium exec "$POD" --json "tar czf /root/out.tgz -C /root/project out" >/dev/null
lium scp "$POD" /root/out.tgz ./out.tgz -d
```

## 7. Provider nodes (`lium provider`)

`lium provider … --json`, or `LIUM_OUTPUT=json` in the environment, prints one envelope per command on stdout: `{"ok": true, "data": …}`, or on failure `{"ok": false, "error": {"code", "legacy_code", "message", "hint", "exit_code", "context", "data"?}}`. `code` is namespaced snake_case (`auth.expired`, `input.arg_invalid`, `portal.not_found`, `ssh.unreachable`, …) and never holds a space; `legacy_code` is the UPPER_CASE code older scripts matched (`PORTAL_NOT_FOUND`), and `null` on a code that never had one (`node.blocked.*`). `data` carries the error's details when there are any. `legacy_code` and `context` are on every provider error, `node.blocked.*` included: `context` holds the same details as `data`, is `{}` when there are none, and is kept for older readers, so read `data`. The provider commands keep their own exit statuses: 1 input, 2 auth, 3 portal, 5 ssh, 6 config, 7 token-cache contention, and 10 for a blocked node. These numbers are today's: a shared exit table for every command is coming in [docs/exit-codes.md](exit-codes.md) and may move them, so until it lands match on `code` and on the `exit_code` the envelope reports.

What keeps a node off the listing or out of idle pay is in `lium provider --json node get <id>` under `data.blocking_reasons`, one entry per reason: `kind` (`idle_pay`, `availability`, `last_error`), `code`, `gating`, `message`, `measured`, `required`, `fix`, `fix_command`, `verify_command`, `requires` (`sudo`, `reboot`, `no_rentals`) and `docs_url`, as the portal sends them. `availability` and `last_error` reasons block renting whatever their `gating`. For `idle_pay`, `gating: true` blocks and `gating: false` only says the node earns no idle pay and needs no action. From a portal that sends no `gating`, the CLI decides from the code and marks the entry `"gating_source": "cli_legacy_fallback"`.

```bash
# exit 10 while anything blocks; the envelope's error.code is node.blocked.<first reason's code>
lium provider node get "$NODE" --json --fail-on-blocked
# after a fix: one JSON object per refresh, exit 0 once clear, 10 when 30 minutes pass first
lium provider node status "$NODE" --json --watch --until-clear --timeout 1800
# plain --watch with --fail-on-blocked: exit 10 at the first refresh that finds the node blocked
lium provider node status "$NODE" --json --watch --fail-on-blocked
```

A reason whose `requires` lists `reboot` (or a fix that needs `sudo` on a host you do not control) is a step for a person: stop and hand it over. `no_rentals` means the fix stops every pod on the host, so it waits until no rental runs. The same goes for `"requires_unknown": true`: the CLI built that reason itself (a node marked `"blocking_reasons_source": "cli_fallback"`) from a code whose needs nobody has listed, so `requires` is empty without meaning "nothing".

## See also

- "First hour on a Lium pod" in `docs/getting-started.rst`
- README – full command list and SDK quick start
