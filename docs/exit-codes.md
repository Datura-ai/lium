# Exit codes and error envelope

Every `lium` command exits non-zero on failure, and each failure has a stable
exit code, a stable error `code` string, a message, and a hint (what to run
next). The table in `lium/cli/utils.py` is the source of truth; this page
mirrors it and a unit test keeps the two in step.

## Exit codes

| Exit | Constant | Meaning |
|------|----------|---------|
| 0 | — | Success. `rm --all`, `reboot --all` and similar on an empty account are success (nothing to do is the requested state). |
| 1 | `EXIT_GENERAL_ERROR` | A failure with no better classification (a batch that finished with failed items, an unexpected exception, a remote command failing under `exec` is its own exit code instead). |
| 2 | `EXIT_CONFIGURATION_ERROR` | Bad arguments, a missing or unreadable configuration value, no API key, a confirmation or input that could not be asked for because no terminal is attached. |
| 3 | `EXIT_API_ERROR` | The API refused or failed the call (5xx, 404 on a resource, rate limit, any other non-2xx). |
| 4 | `EXIT_SSH_ERROR` | ssh could not connect, the pod has no SSH endpoint yet, or no ssh client is installed. |
| 5 | `EXIT_POD_NOT_FOUND` | The pod, cluster or fabric named on the command line does not exist (`lium clusters rm`: also a cluster the API answered 404 for). |
| 6 | `EXIT_PERMISSION_DENIED` | The account is not allowed to do this: unverified account, insufficient balance, an API key without the scope (403); an API key over its budget (402). Overloaded by `topup card` (not released yet) for "outcome unknown": the answer to the charge was lost (`charge_outcome_unknown`) or the platform answered 202 without a `payment_intent_id` (`charge_pending`) — a person must check the balance before the command runs again. A 202 with a `payment_intent_id` is success (exit 0). A script branching on the exit code alone cannot tell these from a 403; `error.code` in the JSON envelope does. |

`lium exec` exits with the remote command's own exit status, so `lium exec pod
"cmd" && next` behaves like `cmd && next` would on the pod. Usage errors caught
by the argument parser (an unknown option, a missing argument) exit 2 with
click's plain-text usage message; they are raised before the command runs and
are not rendered as JSON. `lium provider …` keeps its own exit-code map,
documented in `lium/cli/provider/_render.py`. `lium mine --register` exits 0
when the node is listed, 1 on a failed step or a fix the portal names, and 2
when the node is registered but not listed — within `--wait` minutes, or not
yet in the node list right after the add — the same
code as a usage error; the timeout message (`Still … after N min`) is on
stdout, a usage error on stderr.

## The error envelope

When a command is run for a machine reader, every failure is one JSON object:

```json
{
  "ok": false,
  "error": {
    "code": "pod_not_found",
    "message": "No pods matching: my-pdo",
    "hint": "Run 'lium ps' to list pods; a name, huid, id or 1-based index is accepted",
    "exit_code": 5
  }
}
```

- `ok` is always `false`; a success payload never has `"ok": false`.
- `code` is a stable `snake_case` identifier to branch on; `message` is for people and may change wording. When the API refused with its own `error.code` (`insufficient_balance`, `pod_not_found`, …) that code is the one you get; the CLI's code for the failure class (table below) otherwise. `exit_code` is always the CLI's, by class.
- `hint` is always present: the next command or option to try. When the API sent a hint with its refusal, that is the one you get; the CLI's own hint for the code otherwise.
- `exit_code` repeats the process exit status for readers that only see the streams.
- `data` (optional) carries anything the caller must not lose along with the failure — `lium signup --json`, for one, returns the credentials it generated; an API refusal puts the server's `request_id` here (also printed as `request_id: …` in the text rendering) to quote to support; `lium up` puts the pod it rented in `data.pod_id` / `data.pod_name` on the failures it raises after the rent (`pod_not_ready`, `pod_start_failed`, `gpu_count_mismatch` with `data.pod_removed` true or false under `--strict-gpus`, `gpu_verification_failed`, `jupyter_install_failed`) and on an API error (`server_error`, `rate_limited`, …) or an API that stops answering (`api_timeout`: the transport error `Lium.ps()` and `schedule_termination` re-raise once their retries run out, `install_jupyter` on the first lost connection) during the wait, the `--ttl` retry or the Jupyter install, because that pod exists and bills; a lost connection on the `--strict-gpus` removal is `gpu_count_mismatch` with `data.pod_removed` false; when `--volume new:…` created a volume, `lium up` puts it in `data.volume_id` (the API id) and `data.volume_huid` (what `--volume id:<HUID>` takes) on `timeout_before_rent` and on the failures at the rent itself (`api_timeout`, `rent_rejected`, and an API error such as `server_error`), because the volume exists and is kept.

The envelope goes to **stderr**, stdout is left empty, and the process exits
with `exit_code`. On success stdout carries the result JSON. Read both streams;
do not `2>/dev/null`.

`lium up --json` acts before it answers, so its progress lines (the node
picked, the rent, the wait, the price prompt) go to stderr and stdout holds
exactly one document, the bare payload, as `ps`, `describe` and `rm --format
json` print theirs (no `ok` wrapper: exit 0 says it worked):

```json
{
  "pod": {"id": "…", "huid": "eager-wolf-aa", "name": "train", "status": "RUNNING", "ssh_cmd": "…", "ssh_command": "…", "ports": {…}, "gpu_type": "…", "gpu_count": 1, "price_per_hour": 0.24, "…": "…"},
  "termination_time": "2026-09-15T12:00:00+00:00"
}
```

`up --json` implies `--no-ssh`: the command ends once the pod is ready, and `pod`
has the keys of one row of `lium ps --format json`. `termination_time` is the
time the backend was given by `--ttl`, `--until` or the `--budget` cap, and is
absent when none was set. Pair `--json` with `--yes`: behind a pipe the price
prompt cannot be asked and the command fails with `confirmation_required`
before anything is rented. The teardown is `lium rm … --format json` (its
`{"removed": […], "failed": […]}` payload carries each pod's uptime and
estimated spend); `rm --json` is a hidden alias for it.

Machine mode is on when any of these holds:

- `--format json` (list commands: `ls`, `ps`, `templates`, `balance`, `describe`, `spend`, and every `clusters` command; and `rm`);
- `--json` (accepted everywhere `--format json` is, and on `up`, `exec`, `describe`, `whoami`, `cp`, `fund`, `signup`, `init`, `audit`, `topup`, `keys`, `workspaces`);
- the environment variable `LIUM_OUTPUT=json` — this switches *failures* to the envelope on every command; success output is JSON only on commands that take `--format json`/`--json`, so pass the flag as well when you need to parse the result.

Without any of these, the same information is printed as text: the error on one
line, the hint dimmed underneath.

## Error codes

Codes raised by the shared error handler (any command can produce them) when the API sent no code of its own; the exit code holds either way:

| `code` | Exit | When | Hint |
|--------|------|------|------|
| `no_api_key` | 2 | No API key in `LIUM_API_KEY` or `~/.lium/config.ini`. | Set `LIUM_API_KEY`, or run `lium init` (headless: `lium init --api-key <key>` with a key from https://lium.io/api-keys, or `lium init --no-browser`, then `lium init --session <ID>`); no account yet? `lium signup --email you@example.com` creates one and stores its key. |
| `invalid_api_key` | 3 | The API answered 401. | `lium config get api.api_key` shows which key is in use; new keys at https://lium.io/api-keys. |
| `session_required` | 3 | A session-only command (`lium keys …`, the `lium workspaces` writes) ran without a session token, the token was refused (expired), or `lium workspaces login` was refused. | `lium workspaces login` (or set `LIUM_SESSION_TOKEN`); an API key cannot fix this. |
| `value_error` | 2 | A value the command received was invalid (SDK `ValueError`). | Check the options. |
| `invalid_arguments` | 2 | Options that contradict each other or a malformed value. | `lium <command> --help`. |
| `confirmation_required` | 2 | A yes/no question could not be asked: no terminal can answer, or the terminal went away mid-prompt (`ui.confirm`); `lium rm` without `--yes` when stdin is not a terminal, whatever the target; `lium topup card --json` (not released yet) without `--yes`, terminal or not. | Re-run with `--yes`. |
| `input_required` | 2 | A value would have been prompted for and no terminal can answer (`ui.prompt`, DAH-2883). | Pass it as an option. |
| `permission_denied` | 6 | The API answered 403. | `lium balance`; verification on https://lium.io. |
| `insufficient_balance` | 6 | 403 whose `error.code` is `insufficient_balance` (the platform's structured error body, lium-platform#210), or, from an older server, whose message says "Insufficient balance"; the SDK raises `LiumInsufficientBalanceError` with `required`/`available` parsed from the message when the server stated them. | `lium topup` or `lium fund`, or a cheaper node (`lium ls --sort price_total`). |
| `budget_exceeded` | 6 | 402 from a route the key's budget guards — a rent, a pod's schedule change (`rm --in/--at`), a top-up (`fund`, `topup`, `topup card`): the API key's daily, monthly or lifetime budget (`lium keys create --daily-budget / --monthly-budget / --max-budget`, `lium keys budget`) is reached, so the request is refused — the message names the window hit — and the billing tick deletes the key's pods (data outside a volume is lost) (needs a server with per-key budgets, not on lium.io yet; the server's own `error.code` is `API_KEY_BUDGET_EXCEEDED` and is the code you get when it sends one). The SDK raises `LiumBudgetExceededError` with `window` (`daily` / `monthly` / `max`), `budget_usd`, `spent_usd` and `api_key_id` when the server stated them; the envelope's `data` repeats them. | `lium keys show <name>` shows the figures; raise or clear the budget with `lium keys budget <name>` (a signed-in session) or use another key. |
| `missing_scope` | 6 | 403 whose message says the API key "does not have the '<scope>' scope": the key lacks `read`, `rent`, `manage` or `billing` for this route; the SDK raises `LiumScopeError` with `scope` set, and the envelope's `data.scope` names it. | `lium keys scopes` explains each scope; `lium keys create <name> --scope <scope>` mints a key that holds it. |
| `pod_not_found` | 5 | The pod named on the command line matched nothing. | `lium ps`. |
| `not_found` | 3 | The API returned 404 for a resource other than the target pod. | List it again and retry. |
| `rate_limited` | 3 | The API returned 429. | Wait and retry with back-off. |
| `server_error` | 3 | The API returned 5xx. | Retry; `LIUM_DEBUG=1` prints the traceback on stderr. |
| `lium_error` | 3 | Any other API failure. | Retry; `LIUM_DEBUG=1` prints the traceback on stderr. |
| `ssh_unavailable` | 4 | The pod has no SSH endpoint yet. | Wait for `lium ps` to show it RUNNING with an SSH command. |
| `ssh_connection_failed` | 4 | ssh could not connect to a RUNNING pod. | Check `lium config get ssh.key_path` and `lium ssh-keys`. |
| `copy_failed` | 1 | `lium cp`: the copy failed on a pod — rsync missing on either side, the one-off transfer key could not be authorised, or rsync exited non-zero; the message carries the pod's stderr. | Both pods need rsync (`apt-get install -y rsync`) and the destination needs `flock` (util-linux); fix what the message names and re-run. |
| `ssh_host_key_unknown` | 4 | `lium cp`: the destination pod's host key was never pinned under `~/.lium/known_hosts/`, so the source pod cannot verify it; nothing was copied. | Connect to the destination once with `lium ssh <huid>` (pins its key), or set `LIUM_SSH_INSECURE=1` to skip host key checks. |
| `ssh_host_key_changed` | 4 | The pod presented an ssh host key that differs from the one pinned under `~/.lium/known_hosts/` (`lium exec` on one pod; SDK callers of `Lium.exec`, `scp`, `download`). | Not a retry: if the pod was rebooted or re-templated and the new key is trusted, delete the file the message names and reconnect. |
| `unexpected_error` | 1 | Anything not classified above. | `LIUM_DEBUG=1` prints the traceback on stderr; report the issue. |

Commands add their own codes for the failures only they can have — for example
`up` raises `node_selection_failed`, `template_failed`, `jupyter_install_failed`,
`unreadable_dockerfile`; `exec` raises `unreadable_script`; `rm` raises
`removal_failed`; `fund` raises `transfer_failed`; `topup card` (not released yet) passes on the platform's own
`CARD_AUTHENTICATION_REQUIRED`, `CARD_DECLINED`, `NO_SAVED_CARD` and
`NO_DEFAULT_CARD` (3: the
API refused the charge and the balance did not move; `data` carries `dashboard_url`, the bank's
`decline_code` and the `payment_intent_id`) and raises `charge_outcome_unknown` (6: a timeout or
5xx after the charge was posted — it may have gone through; the message says to check `lium
balance` before trying again, `data.idempotency_key` is the key a repeat must carry with the
same amount within 24 h to get the
same charge back; after that, check `lium balance` before repeating) and `charge_pending` (6 too: a 202 `processing` with no `payment_intent_id` —
the outcome is not known, the charge may never have happened; a 202 with a `payment_intent_id`
is success, exit 0; `data` is the server's
answer, `status: processing`, the key and the amount included, and a repeat with the key and the
same amount within 24 h shows the charge's
status without making a second one); `init` raises `api_unreachable` (3, the
key passed with `--api-key` was not checked) and `empty_api_key` (2), and its `invalid_api_key` hint says
nothing was saved. They follow the same envelope
and use the exit code of their family from the table above. Where re-running
the command would not be safe the hint says so: `jupyter_install_failed` from
`up` points at `lium update <pod> --jupyter` (the pod exists and bills), and
`transfer_failed` says to check the wallet and `lium balance` before funding
again (the transfer may have reached the chain).

With `LIUM_DEBUG=1` every handled failure also prints its Python traceback to
stderr before the error (or the envelope), so "re-run with `LIUM_DEBUG=1`" gives
the detail the hints promise.

## Programmatic use

```bash
set -o pipefail
if ! out=$(LIUM_OUTPUT=json lium ps --format json 2>err.json); then
  code=$(jq -r .error.code err.json)
  hint=$(jq -r .error.hint err.json)
  echo "lium failed: $code — $hint" >&2
  exit $(jq -r .error.exit_code err.json)
fi
echo "$out" | jq '.[0].huid'
```

```python
import json, os, subprocess

proc = subprocess.run(
    ["lium", "ps", "--format", "json"],
    capture_output=True, text=True, env={**os.environ, "LIUM_OUTPUT": "json"},
)
if proc.returncode != 0:
    error = json.loads(proc.stderr)["error"]
    raise RuntimeError(f"{error['code']}: {error['message']} ({error['hint']})")
pods = json.loads(proc.stdout)
```
