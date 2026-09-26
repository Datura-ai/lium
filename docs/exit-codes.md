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
are not rendered as JSON. `lium provider …` and `lium mine --json` use the
provider map below ([Provider commands](#provider-commands-lium-provider-and-lium-mine---json)).
`lium mine --register` in text mode exits 0
when the node is listed, 1 on a failed step or a fix the portal names, and 2
when the node is registered but not listed — within `--wait` minutes, or not
yet in the node list right after the add — the same
code as a usage error; the timeout message (`Still … after N min`) is on
stdout, a usage error on stderr. Under `--json` the not-listed case is exit 11.

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

## Provider commands (`lium provider …` and `lium mine --json`)

Provider commands print their envelope on **stdout** (`{"ok": true, "data": …}` or
`{"ok": false, "error": {"code", "message", "hint", "exit_code", "data"?, "request_id"?}}`);
progress, when a command has any, is one JSON object per line on stderr. `--json` or
`LIUM_OUTPUT=json` turns it on; every command that changes something takes `--yes`.

### Which exit map applies

- **Under `--json` or `LIUM_OUTPUT=json`**, every provider error exits by the unified map below, whatever its
  origin. `error.code` is the namespaced code and `error.legacy_code` the UPPER_CASE code it replaces
  (`PORTAL_REQUEST_REJECTED`, `PORTAL_NOT_FOUND`, …), so a script can still match the old name.
- **In text mode** (no `--json`), every error keeps the exit status it had before the unified map, so
  existing scripts don't break, and stderr prints the UPPER_CASE code and the wording it always had
  (`[PORTAL_SERVER_ERROR] network error reaching portal: …`, `[PORTAL_REQUEST_REJECTED] …`); the namespaced
  codes appear only under `--json`. The status follows the error's UPPER_CASE code:

| Old code | Text exit | `--json` code | `--json` exit |
|----------|-----------|---------------|---------------|
| `ARG_INVALID` | 1 | `input.arg_invalid` (no sign-in at all: `auth.not_signed_in`, exit 6) | 2 |
| `PORTS_INVALID` | 1 | `input.ports_invalid` | 2 |
| `HOTKEY_NOT_REGISTERED` | 1 | `auth.hotkey_not_registered` | 6 |
| `PORTAL_REQUEST_REJECTED` | 1 | `portal.request_rejected`, or the portal's own `portal.<code>` | 3 |
| `PORTAL_AUTH_INVALID` | 2 | `auth.invalid`, or `portal.<code>` | 6 |
| `PORTAL_AUTH_EXPIRED` | 2 | `auth.expired`, or `portal.<code>` | 6 |
| `WALLET_NOT_FOUND` | 2 | `auth.wallet_not_found` | 5 |
| `PORTAL_FORBIDDEN` | 2 | `auth.forbidden`, or `portal.<code>` | 6 |
| `PORTAL_SERVER_ERROR` | 3 | `portal.server_error`, or `net.unreachable` (4) | 3 |
| `PORTAL_NOT_FOUND` | 3 | `portal.not_found`, or `portal.<code>` | 5 |
| `PORTAL_CONTRACT_DRIFT` | 3 | `portal.contract_drift` | 3 |
| `PORTAL_RATE_LIMIT` | 3 | `portal.rate_limited`, or `portal.<code>` | 7 |
| `SSH_UNREACHABLE` | 5 | `ssh.unreachable` | 4 |
| `SSH_AUTH_FAILED` | 5 | `ssh.auth_failed` | 4 |
| `CONFIG_MISSING` | 6 | `input.config_missing` | 2 |
| `PORTAL_AUTH_REFRESH_RACE` | 7 | `auth.refresh_race`: another process holds the token cache; retry | 7 |
| `INSTALLER_PARTIAL_FAIL` | 1 | `host.installer_partial_fail` | 1 |
| `EXECUTOR_UUID_MISMATCH` | 1 | `host.executor_uuid_mismatch` | 1 |
| `UUID_NOT_FOUND` | 1 | `host.uuid_not_found` | 5 |

  The same applies to `lium mine status --json`, which prints the provider envelope (an SS58 given as the hotkey
  name is `ARG_INVALID`: 1 in text, 2 under `--json`).

  In text mode the persona gate prompts as it always has: off a terminal it reads the answer from stdin, so a
  piped `y` confirms, and a decline, EOF (Ctrl-D) or Ctrl-C exits 1. Under `--json`, `LIUM_OUTPUT=json` or
  `LIUM_NONINTERACTIVE=1` (agent mode) piped input is ignored: the gate fails with
  `input.confirmation_required` (exit 2); use `--yes` or `LIUM_PROVIDER_ACK=1`. Ctrl-C at any point of a
  `lium provider` command under `--json` is `input.interrupted` (exit 130); in text mode it is click's
  `Aborted!` (exit 1), as it always was.

  A `lium provider` code with no old equivalent (`input.confirmation_required`, `human.handoff_required`,
  `human.handoff_expired`, `portal.not_supported`) has `legacy_code: null` and exits by the unified map in both
  modes; `input.interrupted` (also `legacy_code: null`) occurs only under `--json`.

### The unified exit map

A namespaced code (`<namespace>.<snake_case>`) exits by this map (`unified_exit_code` in
`lium/provider/errors.py`):

| Exit | Constant | Meaning |
|------|----------|---------|
| 0 | `EXIT_OK` | Success. |
| 1 | `EXIT_GENERAL` | A failure with no better class; every `host.*` step failure of `lium mine`. |
| 2 | `EXIT_INPUT` | `input.*`: a value or a confirmation the command does not ask for in agent mode (`--json`, `LIUM_NONINTERACTIVE=1`), a bad option or token. |
| 3 | `EXIT_API` | `portal.*`: the portal refused or failed the call; `portal.not_supported` (the portal does not serve this route yet). |
| 4 | `EXIT_NETWORK` | `net.*` and `ssh.*`: nothing answered (`net.unreachable`: connection refused, DNS, timeout). |
| 5 | `EXIT_NOT_FOUND` | A code ending in `not_found`, or a portal 404 (`node.not_found`, `portal.executor_not_found`). |
| 6 | `EXIT_AUTH` | `auth.*`, or a portal refusal with HTTP 401/403/419/440. |
| 7 | `EXIT_RETRYABLE` | A portal 429: retry after a pause. |
| 10 | `EXIT_BLOCKED` | `node.blocked.<code>`: the portal refused the node change for a named reason. |
| 11 | `EXIT_NOT_LISTED` | `node.not_listed_yet`: the node is registered and waited for, but not listed. |
| 12 | `EXIT_HUMAN` | `human.*`: a person has to act (link Discord, confirm the e-mail) before the command can succeed; `data` says what to relay. |
| 130 | `EXIT_INTERRUPTED` | `input.interrupted`: Ctrl-C under `--json`. |

A portal refusal whose body names `detail.code` is passed through as `portal.<code>`, the portal's code in
snake_case (`EXECUTOR_NOT_FOUND` is `portal.executor_not_found`, `NODE_RENTED` is `portal.node_rented`), with
`detail.message` as the message, the portal's own spelling in `error.data.portal_code` and the rest of `detail`
in `error.data.detail`. Its `legacy_code` is the UPPER_CASE code the same status had before (`PORTAL_REQUEST_REJECTED`
for a 400 or 409, `PORTAL_FORBIDDEN` for a 403, `PORTAL_NOT_FOUND` for a 404, `PORTAL_RATE_LIMIT` for a 429).

| `code` | Exit | When |
|--------|------|------|
| `input.confirmation_required` | 2 | The persona gate under `--json`, `LIUM_OUTPUT=json` or `LIUM_NONINTERACTIVE=1`, where piped input is ignored. Re-run with `--yes` or `LIUM_PROVIDER_ACK=1`. |
| `input.interrupted` | 130 | Ctrl-C during a `lium provider` command under `--json`. |
| `input.input_required` | 2 | A value the command does not ask for: `lium mine` without `-k` under `--json` or `LIUM_NONINTERACTIVE=1`, `portal login --email` without `LIUM_PROVIDER_PASSWORD` off a terminal. |
| `input.register_token_invalid` | 2 | `lium mine --json --register` with an expired or unreadable token; nothing on the host was touched. |
| `input.hotkey_conflicts_with_token` | 2 | `lium mine --json --register … -k` with a hotkey the token does not name. |
| `human.handoff_required` | 12 | A one-time human step (`lium provider config connect-discord`, `lium provider portal confirm-email`). `data`: `step` (`discord_link`, `email_confirm`), `handoff_url`, `code`, `expires_at`, `message_for_human`. Relay `message_for_human` to the person, then re-run with `--wait`. With `--wait --timeout N`, also when N seconds pass first (`data.waited_s`). |
| `human.handoff_expired` | 12 | `--wait`: the code expired before the person finished, or the portal answers 404 for the handoff (`data.status: not_found`); run the command again for a new code. A poll the portal could not answer (5xx, 429, unreachable) is retried with backoff until `--timeout`. |
| `auth.not_signed_in` | 6 | Any `lium provider` command that needs a sign-in, in agent mode, with no `LIUM_PROVIDER_TOKEN`, no hotkey and no live `portal login --email` session (`data.session_email` names a session that has ended); `lium mine status` too. The hint names all three ways in; `legacy_code` is `ARG_INVALID`. Text mode prints `ARG_INVALID` and exits 1 as before (`portal whoami` says "not signed in to the provider portal" with the same hint). `status`, `portal login` and `portal logout` need the hotkey itself and stay `input.arg_invalid`. |
| `net.unreachable` | 4 | Connection refused, DNS failure or timeout reaching the portal. |
| `portal.not_supported` | 3 | The portal answered 404/405 for a route this CLI knows (`lium provider token …` before the portal serves API tokens). |
| `portal.api_token_needs_session` | 6 | `lium provider token create`, `list` or `revoke` sent `LIUM_PROVIDER_TOKEN`; tokens are managed from a signed-in session only (hotkey or `portal login --email`). |
| `portal.api_token_scope_missing` | 6 | `LIUM_PROVIDER_TOKEN` lacks the scope the call needs; `data.detail.required_scopes` names it. |
| `portal.overview_not_for_custodied_account` | 6 | `lium provider idle-pay` on an account created with e-mail or Google: the portal serves its overview to hotkey accounts only (text mode: exit 2, as `PORTAL_FORBIDDEN`). |
| `portal.<code>` | by status | The portal's own code in snake_case (`portal.node_rented`), passed through; `legacy_code` is the old code for its status. |
| `node.not_found` | 5 | `node listing` / `idle-pay` named a node the account does not have. |
| `node.not_listed_yet` | 11 | `lium mine --json --register`: registered, not listed within `--wait`. |
| `node.<status>` | 1 | `lium mine --json --register`: the portal names a fix (`node.offline`, `node.validation_failed`). |
| `host.nvidia_driver_missing`, `host.nvidia_container_toolkit_missing`, `host.docker_missing` | 1 | `lium mine` step 3. |
| `host.port_in_use` | 1 | `lium mine` step 4: `data` has `port`, `label`, `owner`. |
| `host.executor_unhealthy` | 1 | `lium mine` step 5: the health check timed out; the message has the compose status and log tail. |
| `host.preflight_failed`, `host.preflight_no_verdict` | 1 | `lium mine` step 6; `data.verdict` is the preflight image's verdict. |
| `host.<step>_failed` | 1 | Any other failure of a step (`host.repo`, `host.tools`, `host.prereqs`, `host.env`, `host.start`, `host.validate`, `host.register`). |

`lium mine --json` step events look like
`{"event": "step", "step": 4, "total": 6, "code": "host.env", "message": "Configuring environment", "status": "started|done|failed", "elapsed_s": 1.2, "error_code": "host.port_in_use"}`;
step 6 adds `{"event": "check", "name": …}` lines and `--register` adds `{"event": "node_status", "node_id", "status", "message"}`.
`--auto` takes the default ports (service 8080, SSH 2200); under `--json`, `LIUM_OUTPUT=json` or
`LIUM_NONINTERACTIVE=1` the defaults are taken anyway and a missing `-k` is `input.input_required`.

Provider auth without a wallet: `LIUM_PROVIDER_TOKEN` is sent as the Bearer token when set;
`lium provider portal login --email you@example.com` reads the password from `LIUM_PROVIDER_PASSWORD`
(a hidden prompt on a terminal) and keeps the session for later commands (`LIUM_PROVIDER_EMAIL` picks the session).
Google sign-in is for people in the portal. An agent signs in with `LIUM_PROVIDER_TOKEN`: a person signed in creates the
token with `lium provider token create` and hands it over.

When more than one is set, a command signs in with the first of: `LIUM_PROVIDER_TOKEN`, the hotkey (`--hotkey`,
`LIUM_PROVIDER_HOTKEY` or `provider.hotkey`), the `portal login --email` session. In agent mode (`--json`,
`LIUM_OUTPUT=json`, `LIUM_NONINTERACTIVE=1`) and in text mode, `lium provider portal whoami` works with any of them.
Under `--json` it adds `auth_method` to the account fields of `/auth/me` (`miner_id`, `miner_hotkey`, `email`, …):
`token`, `hotkey` or `email_session`, the one used. The text output names the sign-in on its first line
(`portal session active, signed in by API token (LIUM_PROVIDER_TOKEN)`) and adds an `Auth Method` row; signed in by
the hotkey, it prints what it always has. With none it is `auth.not_signed_in` (exit 6; text: `ARG_INVALID`, exit 1).

`lium provider node listing [NODE_ID] --json` rows always carry `gpu_count` and `rented_gpu_count` (`null` when the
portal does not send them). A node rented in part keeps `listing_state: "listed"` for its free GPUs, with
`rented_gpu_count` above 0: check `rented_gpu_count`, not only `listing_state`, before anything that interrupts a rental.

One-time human steps (`config connect-discord`, `portal confirm-email`) answer `human.handoff_required` (exit 12):
one URL plus a short code for the person. `config connect-discord` does so with `--wait` or in agent mode
(`--json`, `LIUM_OUTPUT=json`, `LIUM_NONINTERACTIVE=1`); in text mode without `--wait` it opens the Discord
authorization URL and waits, as it always has. `portal confirm-email` is new and uses the handoff in both modes. `--wait` polls until the step is done (exit 0) or the code expires
(`human.handoff_expired`, exit 12); under `--json` the handoff is also one `{"event": "handoff", …}` line on stderr
while it waits. The person enters the code in the portal, which then starts the step (the Discord consent, or the
confirmation link mailed to the address). A portal that does not serve handoff sessions answers `portal.not_supported` (exit 3) with
`data.legacy_flow: true` and `data.legacy_browser_url`, the old browser link; in text mode `connect-discord --wait`
then runs the old browser flow.

### Old codes and their new names

In text mode the UPPER_CASE codes keep their old exit statuses (1 argument, 2 auth, 3 portal, 5 SSH, 6 config,
7 token-cache race), so scripts keep working while they migrate. The failures below carry the namespaced code,
with the old one as `legacy_code`; the exits on the right are the `--json` ones:

| Old `code` (exit) | New `code` (exit) | When |
|-------------------|-------------------|------|
| `PORTAL_SERVER_ERROR` (3) | `net.unreachable` (4) | Connection refused, DNS failure, timeout. A 5xx stays `PORTAL_SERVER_ERROR`. |
| `PORTAL_REQUEST_REJECTED` (1), `PORTAL_FORBIDDEN` (2), `PORTAL_NOT_FOUND` (3), `PORTAL_RATE_LIMIT` (3) | `portal.<code>` (3, 6, 5, 7) | The portal's body named `detail.code` (`node rm` on a rented node, the `node add` refusals). Text mode keeps the old exit. |
| the persona prompt | `input.confirmation_required` (2) | Under `--json`, `LIUM_OUTPUT=json` or `LIUM_NONINTERACTIVE=1` piped input is ignored; use `--yes` or `LIUM_PROVIDER_ACK=1`. Text mode prompts as it always has; a decline stays `ARG_INVALID` (1). |
| `ARG_INVALID` (1) | `auth.not_signed_in` (6) | A command that needs a sign-in found none, in agent mode (`--json`, `LIUM_OUTPUT=json`, `LIUM_NONINTERACTIVE=1`; under `LIUM_NONINTERACTIVE=1` alone the output is text, so the exit stays 1). Text mode keeps the old `… require --hotkey` line. |
| exit 1 (Ctrl-C) | `input.interrupted` (130) | Under `--json` only; text mode is unchanged. |
| `PORTAL_AUTH_REFRESH_RACE` (7) | `auth.refresh_race` (7) | Another process holds the token cache; retry. |
| exit 2 (text, not listed) | `node.not_listed_yet` (11) | `lium mine --register`, under `--json` only. |
| exit 1 (any `lium mine` step) | `host.*` (1) | Under `--json` only; text mode is unchanged. |
| exit 0 with `authorization_url` | `human.handoff_required` (12), or `portal.not_supported` (3) with `data.legacy_browser_url` | `config connect-discord --json`: exit 3 until the portal serves handoff sessions. |

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
