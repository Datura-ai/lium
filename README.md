# Lium CLI

Command-line interface for managing GPU pods on the Lium platform.

<p align="center">
  <img src="assets/web-app-logo.png" alt="Lium CLI Logo" width="120" />
</p>

<h1 align="center">Lium</h1>

<div align="center">
  <a href="https://docs.lium.io/cli/quickstart">Quickstart</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://lium.io/?utm_source=github">Website</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://docs.lium.io/cli">Docs</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://docs.lium.io/cli/commands">Reference</a>
  <span>&nbsp;&nbsp;•&nbsp;&nbsp;</span>
  <a href="https://discord.gg/lium">Discord</a>
</div>

## Installation

### Python package

```bash
pip install lium.io
```

### Binary install (macOS amd64/arm64 / Linux amd64/arm64)

```bash
curl -fsSL https://github.com/Datura-ai/lium-cli/releases/latest/download/install.sh | bash
```

## Quick Start

```bash
# First-time setup
lium init

# List available executors (GPU machines)
lium ls

# Create a pod using executor index
lium up 1  # Use executor #1 from previous ls

# Or create a pod using filters
lium up --gpu A100  # Auto-select best A100 executor

# List your pods
lium ps

# Copy files to pod
lium scp 1 ./my_script.py

# SSH into a pod
lium ssh <pod-name>

# Stop a pod
lium rm <pod-name>
```


## Documentation

The SDK and decorator API ship with Sphinx-powered docs that are ready for Read the
Docs. Build them locally with:

```bash
pip install -e .[docs]
sphinx-build -b html docs docs/_build/html
```

The generated HTML lives in `docs/_build/html`. Publishing to Read the Docs only
requires connecting this repository; the `.readthedocs.yaml` file points RTD at
`docs/conf.py`.

## Binary Releases

- Supported binary targets: `darwin-amd64`, `darwin-arm64`, `linux-amd64`, `linux-arm64`
- Maintainers can build locally with `bash scripts/build.sh [macos|linux|all]`
- Release artifacts publish through GitHub Releases with matching checksums


 ![Area](https://github.com/user-attachments/assets/089e3a25-f246-4664-a069-1366d8357fe3)


## Commands

### Core Commands

- `lium init` - Initialize configuration (API key, SSH keys)
- `lium ls [GPU_TYPE]` - List available executors
- `lium up [EXECUTOR_ID]` - Create a pod (use executor ID or filters like `--gpu`, `--count`, `--country`)
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

### Other Commands

- `lium theme [THEME]` - Get or set UI theme (light/dark/auto)
- `lium mine` - Set up a compute subnet executor/miner
- `sudo lium gpu-splitting setup [--device /dev/...] [--yes]` - Prepare Docker storage for LIUM GPU splitting
- `lium gpu-splitting check [--device /dev/...]` - Inspect the host and print the GPU-splitting plan
- `lium gpu-splitting verify` - Verify Docker storage matches LIUM GPU-splitting requirements

### Command Examples

```bash
# Filter executors by GPU type
lium ls H100
lium ls A100

# Create pod with executor index
lium up 1 --name my-pod --yes

# Create pod with filters (auto-selects best executor)
lium up --gpu A100 --count 8 --name my-pod --yes
lium up --gpu H200 --country US

# Create pod with specific template
lium up 1 --template_id <TEMPLATE_ID> --yes

# Set up executor bootstrap flow
lium mine --auto --hotkey <HOTKEY>

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

- **Pareto Optimization**: `ls` command shows optimal executors with ★ indicator
- **Flexible Pod Creation**: Use executor index or auto-select with filters (GPU type, count, country)
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
git clone https://github.com/Datura-ai/lium-cli.git
cd lium-cli

# Install in development mode
pip install -e .
```

## License

MIT License - see [LICENSE](LICENSE) file for details.
