# Lium for agents and scripts

One page for an LLM agent (or any unattended script) that has to rent a GPU pod, run work on it, collect the results and give the pod back, without a human at the keyboard. Everything here is a shell command plus `jq`, or the Python SDK; nothing needs a terminal. Every `lium …` line on this page, and the list of `--json` commands in §2, is resolved against this version's command tree by `test/test_agent_docs.py`; check `lium <command> --help` for anything not covered here.

The rules of the road:

1. **Authenticate from the environment.** `LIUM_API_KEY` wins over `~/.lium/config.ini`. Never run `lium init` from an agent.
2. **Ask for JSON.** `--format json` on `ls`/`ps`, `--json` on `exec`/`describe`/`balance`: the result is on stdout and the exit code says whether it worked. On any of them a runtime error is one JSON object on stderr (`LIUM_OUTPUT=json` switches that on for every command); a usage error is click's plain text with exit 2 (§2).
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

Failure on a renter command that takes `--json` (`exec`, `describe`, `balance`, `whoami`, `audit`; `fund`, `signup`, `topup currencies` and `topup create` too): stdout is empty, **stderr** holds one JSON object, the exit code is non-zero:

```json
{"ok": false, "error": {"code": "pod_not_found", "message": "No pods match targets: train-1", "hint": "Run 'lium ps' to list pods; a name, huid, id or 1-based index is accepted", "exit_code": 5}}
```

All four keys are always there: `error.hint` is the next command or option to try, `error.exit_code` repeats the process exit code. The codes and hints are listed in [docs/exit-codes.md](exit-codes.md).

A usage error on any command — an unknown option, a missing argument — is click's plain-text `Usage: … Error: No such option '--bogus'` on stderr with exit 2, not the envelope: read exit 2 with non-JSON stderr as "fix the invocation".

Failure on `ls --format json` / `ps --format json`: the same envelope on **stderr**, stdout empty — `lium ps no-such-pod --format json` exits 5 with `{"ok": false, "error": {"code": "pod_not_found", …}}` on stderr. `LIUM_OUTPUT=json` in the environment turns the envelope on for every command's failures, flag or no flag; success output stays JSON only where `--format json`/`--json` asks for it.

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

`lium up` has no JSON output, so name the pod yourself and read it back from `lium ps`:

```bash
NAME="job-$(date +%s)"
lium up "$NODE" --name "$NAME" --ttl 4h --yes --no-ssh
POD=$(lium ps --format json | jq -r --arg n "$NAME" '.[] | select(.name==$n) | .huid')
```

`--yes` skips the price confirmation, `--no-ssh` returns instead of opening a shell, `--ttl` is the safety net. `up` waits until SSH is reachable before returning. Check the GPU count yourself before spending on the job:

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

`lium ps --format json` / `lium ls --format json` return the table-equivalent objects shown above, and `lium describe <pod> --json` returns the full pod manifest.

## 6. A complete run

```bash
#!/usr/bin/env bash
set -euo pipefail

NODE=$(lium ls --gpu H100 --count 1 --format json | jq -r '.[0].huid')
NAME="job-$(date +%s)"
lium up "$NODE" --name "$NAME" --ttl 4h --yes --no-ssh
POD=$(lium ps --format json | jq -r --arg n "$NAME" '.[] | select(.name==$n) | .huid')
trap 'lium rm "$POD" --yes >/dev/null 2>&1 || true' EXIT

lium rsync "$POD" ./project /root/project
lium exec "$POD" --json "cd /root/project && python -m venv .venv && . .venv/bin/activate && pip install -q -r requirements.txt" >/dev/null
lium exec "$POD" --json "cd /root/project && . .venv/bin/activate && python train.py --epochs 1"
lium exec "$POD" --json "tar czf /root/out.tgz -C /root/project out" >/dev/null
lium scp "$POD" /root/out.tgz ./out.tgz -d
```

## See also

- "First hour on a Lium pod" in `docs/getting-started.rst`
- README – full command list and SDK quick start
