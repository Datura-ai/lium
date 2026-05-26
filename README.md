# Lium — Python SDK & CLI

`lium.io` is a Python package that provides both a **command-line interface** and a **Python SDK** for managing GPU pods on the [Lium](https://lium.io) platform. Install it once — use whichever interface fits the job.

<p align="center">
  <img src="assets/web-app-logo.png" alt="Lium Logo" width="120" />
</p>

<h1 align="center">Lium</h1>

<div align="center">
  <a href="https://docs.lium.io/cli/quickstart">Quickstart</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://lium.io/?utm_source=github">Website</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://docs.lium.io/category/cli">CLI Docs</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://docs.lium.io/developers/sdk">SDK Docs</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://discord.gg/lium">Discord</a>
</div>

![Lium](https://github.com/user-attachments/assets/089e3a25-f246-4664-a069-1366d8357fe3)

## Installation

### Python package

```bash
pip install lium.io
```

### Binary install (macOS amd64/arm64 / Linux amd64/arm64)

```bash
curl -fsSL https://lium.io/install.sh | bash
```

Fresh binary installs place a managed symlink at `~/.lium/bin/lium` that points to a
versioned binary under `~/.lium/versions/<version>/lium`.

## Quick Start

### CLI

```bash
# First-time setup
lium init

# List available nodes (GPU machines)
lium ls

# Create a pod using node index
lium up 1  # Use node #1 from previous ls

# Or create a pod using filters
lium up --gpu A100  # Auto-select best A100 node

# List your pods
lium ps

# Copy files to pod
lium scp 1 ./my_script.py

# SSH into a pod
lium ssh <pod-name>

# Stop a pod
lium rm <pod-name>
```

### SDK

The SDK mirrors the CLI's capabilities for programmatic use. Two entry points: the `@lium.machine` decorator for quickly offloading isolated functions, and the `Lium()` client for long-lived orchestration code.

High-level decorator — annotate a function and offload work to a GPU pod:

```python
import lium

@lium.machine(machine="A100", requirements=["torch", "transformers", "accelerate"])
def infer(prompt: str) -> str:
    from transformers import AutoTokenizer, AutoModelForCausalLM
    tokenizer = AutoTokenizer.from_pretrained("sshleifer/tiny-gpt2")
    model = AutoModelForCausalLM.from_pretrained("sshleifer/tiny-gpt2", device_map="cuda")
    tokens = tokenizer(prompt, return_tensors="pt").to("cuda")
    out = model.generate(**tokens, max_new_tokens=50)
    return tokenizer.decode(out[0], skip_special_tokens=True)

print(infer("Who discovered penicillin?"))
```

Direct SDK usage follows the same pattern:

```python
from lium.sdk import Lium

lium = Lium()
node = lium.ls(gpu_type="A100")[0]
pod = lium.up(executor_id=node.id, name="demo")
ready = lium.wait_ready(pod, timeout=600)
print(lium.exec(ready, command="nvidia-smi")["stdout"])
lium.down(ready)
```

Full API reference: https://docs.lium.io/developers/sdk/reference

## Documentation

- **CLI docs:** https://docs.lium.io/category/cli
- **SDK docs:** https://docs.lium.io/developers/sdk

## Binary Releases

- Supported binary targets: `darwin-amd64`, `darwin-arm64`, `linux-amd64`, `linux-arm64`
- Maintainers can build locally with `bash scripts/build.sh [macos|linux|all]`
- Release artifacts publish through GitHub Releases with matching checksums

## CLI Reference

The `lium` CLI exposes the full pod lifecycle. Run `lium --help` to see everything, or browse the reference below.

### Core Commands

- `lium init` - Initialize configuration (API key, SSH keys)
- `lium ls [GPU_TYPE]` - List available nodes
- `lium up [NODE_ID]` - Create a pod (use node ID or filters like `--gpu`, `--count`, `--country`)
- `lium ps` - List active pods
- `lium ssh <POD>` - SSH into a pod
- `lium exec <POD> <COMMAND>` - Execute command on pod
- `lium scp <POD> <LOCAL_FILE> [REMOTE_PATH]` - Copy files to pods (add `-d` to download from pods)
- `lium rsync <POD> <LOCAL_DIR> [REMOTE_PATH]` - Sync directories to pods
- `lium rm <POD>` - Remove/stop a pod
- `lium reboot <POD>` - Reboot a pod
- `lium update <POD>` - Install Jupyter on a pod
- `lium templates [SEARCH]` - List available Docker templates
- `lium fund` - Fund account with TAO from Bittensor wallet

### Volume Commands

- `lium volumes list` - List all volumes
- `lium volumes new <NAME>` - Create a new volume
- `lium volumes rm <VOLUME>` - Remove a volume

### Backup Commands

- `lium bk show <POD>` - Show backup configuration for a pod
- `lium bk set <POD> <PATH>` - Configure automatic backups
- `lium bk logs <POD>` - View backup logs
- `lium bk now <POD>` - Trigger immediate backup
- `lium bk restore <POD> <BACKUP_ID>` - Restore from backup
- `lium bk rm <POD>` - Remove backup configuration

### Schedule Commands

- `lium schedules list` - List scheduled terminations
- `lium schedules rm <POD>` - Cancel scheduled termination

### Configuration Commands

- `lium config show` - Show all configuration
- `lium config get <KEY>` - Get configuration value
- `lium config set <KEY> <VALUE>` - Set configuration value
- `lium config unset <KEY>` - Remove configuration key
- `lium config edit` - Edit configuration file
- `lium config path` - Show configuration file path
- `lium config reset` - Reset all configuration

### Provider Commands

`lium provider …` is the provider-side CLI for Bittensor Subnet 51 — full automation parity with the portal frontend at lium.io/portal: portal authentication, node lifecycle, central-miner-server configuration, batch sync, billing, and machine-request queries. Hotkey registration is still handled separately via `btcli subnet register`.

Group-level flags inherited by every subcommand: `-w/--coldkey`, `-k/--hotkey`, `--portal-url`, `--json`, `--debug`, `-y/--yes`, `--dry-run`. Persist wallet identity once with `lium config set provider.coldkey <NAME>` and `lium config set provider.hotkey <NAME>`. Spend-affecting subcommands run a persona prompt unless `--yes` or `LIUM_PROVIDER_ACK=1` is set.

- `lium provider portal {login,logout,whoami}` - Manage the cached portal JWT
- `lium provider status [--netuid 51]` - Aggregated provider snapshot (registration, portal session, nodes, validator weights)
- `lium provider node list|get|add|rm|update-price|update-gpu` - Node lifecycle on the portal
- `lium provider node min-gpu set|unset <NODE_ID> [COUNT]` - Min GPU count for rental matchmaking
- `lium provider node pods <NODE_ID>` - Pods currently rented on a node
- `lium provider node machine-requests <NODE_ID>` - Pending tenant requests on a node
- `lium provider node notice-period set|unset <NODE_ID>` - Open/close a maintenance notice period
- `lium provider node notify-added <NODE_ID> --request-id <REQ>` - Mark a tenant machine request fulfilled
- `lium provider config show|opt-in|opt-out|set-email|set-subscriptions` - Portal-account configuration (incl. lium.io central miner server toggle)
- `lium provider sync from-miner-server|to-miner-server` - Batch node-state sync between portal and the central miner server
- `lium provider billing list [--miner-hotkey HK] [--page N] [--limit N]` - Paginated billing history
- `lium provider machine-request list|get` - Pending tenant machine requests
- `lium provider machine list|estimate` - Machine catalogue + reward estimates

Full reference with every flag and runnable examples: <https://docs.lium.io/developers/cli/reference/provider>.

### Other Commands

- `lium theme [THEME]` - Get or set UI theme (light/dark/auto)
- `lium mine` - Set up a compute subnet node/miner
- `sudo lium gpu-splitting setup [--device /dev/...] [--yes]` - Prepare Docker storage for LIUM GPU splitting
- `lium gpu-splitting check [--device /dev/...]` - Inspect the host and print the GPU-splitting plan
- `lium gpu-splitting verify` - Verify Docker storage matches LIUM GPU-splitting requirements

### Command Examples

```bash
# Filter nodes by GPU type
lium ls H100
lium ls A100

# Create pod with node index
lium up 1 --name my-pod --yes

# Create pod with filters (auto-selects best node)
lium up --gpu A100 --count 8 --name my-pod --yes
lium up --gpu H200 --country US

# Create pod with specific template
lium up 1 --template_id <TEMPLATE_ID> --yes

# Set up node bootstrap flow
lium mine --auto --hotkey <HOTKEY>

# Provider-portal automation (same surface as the portal frontend)
lium config set provider.coldkey miner-prod        # one-time: persist wallet identity
lium config set provider.hotkey  miner-1
lium provider portal login                         # JWT exchange via hotkey signature
lium provider status                               # registration, portal session, nodes, weights
lium provider node list --limit 50
lium provider node add --gpu-type "NVIDIA H200 NVL" --gpu-count 8 \
    --ip 203.0.113.42 --port 8080 --price 1.85 --yes
lium provider node update-price <NODE_ID> --price 2.10 --yes
lium provider config opt-in --yes                  # use lium.io's central miner server
lium provider machine estimate --gpu-type "NVIDIA H200 NVL" --gpu-count 8
lium provider --json status                        # JSON envelope for scripts/agents

# Inspect or configure Docker storage for GPU splitting (Ubuntu/Debian + systemd, run setup as root)
lium gpu-splitting check
sudo lium gpu-splitting setup --yes
lium gpu-splitting verify

# Create pod with volume
lium up 1 --volume id:<VOLUME_HUID>
lium up 1 --volume new:name=mydata,desc="My dataset"

# Create pod with auto-termination
lium up 1 --ttl 6h                    # Terminate after 6 hours
lium up 1 --until "today 23:00"       # Terminate at 11 PM today

# Create pod with Jupyter
lium up 1 --jupyter --yes

# Execute commands
lium exec my-pod "nvidia-smi"
lium exec my-pod "python train.py"

# Copy files to and from pods
lium scp my-pod ./script.py                    # Copy to /root/script.py
lium scp 1 ./data.csv /root/data/             # Copy to specific directory
lium scp all ./config.json                    # Copy to all pods
lium scp 1,2,3 ./model.py /root/models/       # Copy to multiple pods
lium scp my-pod /root/output.log ./downloads -d  # Download into ./downloads directory

# Reboot pods
lium reboot my-pod                           # Reboot a single pod
lium reboot 1,2 --yes                        # Reboot pods 1 and 2 without confirmation
lium reboot all                              # Reboot all active pods
lium reboot my-pod --volume-id <VOLUME_ID>   # Reboot with a specific volume ID

# Sync directories to pods
lium rsync my-pod ./project                    # Sync to /root/project
lium rsync 1 ./data /root/datasets/           # Sync to specific directory
lium rsync all ./models                       # Sync to all pods
lium rsync 1,2,3 ./code /root/workspace/      # Sync to multiple pods

# Remove multiple pods
lium rm my-pod-1 my-pod-2
lium rm all  # Remove all pods

# Install Jupyter on existing pod
lium update my-pod

# Manage volumes
lium volumes list
lium volumes new mydata --description "My dataset"
lium volumes rm <VOLUME_HUID>

# Manage backups
lium bk show my-pod
lium bk set my-pod /root/data --frequency 24 --retention 7
lium bk logs my-pod
lium bk now my-pod --name manual-backup
lium bk restore my-pod <BACKUP_ID> /root/restore
lium bk rm my-pod

# Manage schedules
lium schedules list
lium schedules rm my-pod

# Configuration management
lium config show
lium config get api.api_key
lium config set ssh.key_path /path/to/key
lium config edit

# Theme management
lium theme          # Show current theme
lium theme dark     # Set to dark theme
lium theme auto     # Auto-detect based on system

# Fund account with TAO
lium fund                           # Interactive mode
lium fund -w default -a 1.5        # Fund with specific wallet and amount
lium fund -w mywal -a 0.5 -y       # Skip confirmation
```

## Features

- **Dual Interface**: Same package ships both the `lium` CLI and a Python SDK (`lium.sdk.Lium` + `@lium.machine` decorator)
- **Pareto Optimization**: `ls` command shows optimal nodes with ★ indicator
- **Flexible Pod Creation**: Use node index or auto-select with filters (GPU type, count, country)
- **Index Selection**: Use numbers from `ls` output in commands
- **Full-Width Tables**: Clean, readable terminal output
- **Cost Tracking**: See spending and hourly rates in `ps`
- **Interactive Setup**: `init` command for easy onboarding
- **Volume Management**: Create and attach persistent storage volumes
- **Backup & Restore**: Automated backups with configurable frequency and retention
- **Auto-Termination**: Schedule pods to terminate after duration or at specific time
- **Jupyter Integration**: One-command Jupyter installation on pods
- **Theme Support**: Light, dark, or auto-detect themes for better visibility

## Configuration

Configuration is stored in `~/.lium/config.ini`:

```ini
[api]
api_key = your-api-key-here

[ssh]
key_path = /home/user/.ssh/id_ed25519
```

You can also use environment variables:
```bash
export LIUM_API_KEY=your-api-key-here
```

## Requirements

- Python 3.9+

## Development

```bash
# Clone repository
git clone https://github.com/datura-ai/lium.git
cd lium

# Install in development mode
pip install -e .
```


## License

MIT License - see [LICENSE](LICENSE) file for details.
