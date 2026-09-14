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
| 5 | `EXIT_POD_NOT_FOUND` | The pod named on the command line does not exist. |
| 6 | `EXIT_PERMISSION_DENIED` | The account is not allowed to do this: unverified account, insufficient balance (403). |

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
- `data` (optional) carries anything the caller must not lose along with the failure — `lium signup --json`, for one, returns the credentials it generated; an API refusal puts the server's `request_id` here (also printed as `request_id: …` in the text rendering) to quote to support.

The envelope goes to **stderr**, stdout is left empty, and the process exits
with `exit_code`. On success stdout carries the result JSON. Read both streams;
do not `2>/dev/null`.

Machine mode is on when any of these holds:

- `--format json` (list commands: `ls`, `ps`, `templates`, `balance`, `describe`);
- `--json` (accepted everywhere `--format json` is, and on `exec`, `describe`, `fund`, `signup`, `audit`, `topup`);
- the environment variable `LIUM_OUTPUT=json` — this switches *failures* to the envelope on every command; success output is JSON only on commands that take `--format json`/`--json`, so pass the flag as well when you need to parse the result.

Without any of these, the same information is printed as text: the error on one
line, the hint dimmed underneath.

## Error codes

Codes raised by the shared error handler (any command can produce them) when the API sent no code of its own; the exit code holds either way:

| `code` | Exit | When | Hint |
|--------|------|------|------|
| `no_api_key` | 2 | No API key in `LIUM_API_KEY` or `~/.lium/config.ini`. | Set `LIUM_API_KEY`, or run `lium init` (headless: `lium init --no-browser`, then `lium init --session <ID>`); no account yet? `lium signup --email you@example.com` creates one and stores its key. |
| `invalid_api_key` | 3 | The API answered 401. | `lium config get api.api_key` shows which key is in use; new keys at https://lium.io/api-keys. |
| `session_required` | 3 | A session-only command (`lium keys …`, the `lium workspaces` writes) ran without a session token, the token was refused (expired), or `lium workspaces login` was refused. | `lium workspaces login` (or set `LIUM_SESSION_TOKEN`); an API key cannot fix this. |
| `value_error` | 2 | A value the command received was invalid (SDK `ValueError`). | Check the options. |
| `invalid_arguments` | 2 | Options that contradict each other or a malformed value. | `lium <command> --help`. |
| `confirmation_required` | 2 | A yes/no question could not be asked: no terminal can answer, or the terminal went away mid-prompt (`ui.confirm`, DAH-2883). | Re-run with `--yes`. |
| `input_required` | 2 | A value would have been prompted for and no terminal can answer (`ui.prompt`, DAH-2883). | Pass it as an option. |
| `permission_denied` | 6 | The API answered 403. | `lium balance`; verification on https://lium.io. |
| `insufficient_balance` | 6 | 403 whose `error.code` is `insufficient_balance` (the platform's structured error body, lium-platform#210), or, from an older server, whose message says "Insufficient balance"; the SDK raises `LiumInsufficientBalanceError` with `required`/`available` parsed from the message when the server stated them. | `lium topup` or `lium fund`, or a cheaper node (`lium ls --sort price_total`). |
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
`removal_failed`; `fund` raises `transfer_failed`. They follow the same envelope
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
