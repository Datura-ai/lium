"""Executor bootstrap subcommand."""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Tuple

import click
from rich import box
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ..utils import console, handle_errors, timed_step_status


def _get_gpu_info() -> dict:
    """Read GPU model/count from `nvidia-smi`.

    Command run:

    - `nvidia-smi --query-gpu=name --format=csv,noheader`
    """
    out, _ = _run("nvidia-smi --query-gpu=name --format=csv,noheader")
    lines = out.strip().split("\n")
    if lines and lines[0]:
        return {"gpu_count": len(lines), "gpu_type": lines[0].strip()}
    return {"gpu_count": 0, "gpu_type": None}


def _get_public_ip() -> str:
    """Resolve the public IPv4 address using a small fallback list.

    Commands run, in order until one returns a valid IPv4:

    - `curl -4 -s https://api.ipify.org?format=text`
    - `curl -4 -s https://ipv4.icanhazip.com`
    - `curl -4 -s https://ifconfig.me/ip`
    """
    services = [
        "https://api.ipify.org?format=text",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip",
    ]
    for service in services:
        out, _ = _run(f"curl -4 -s {service}")
        ip = out.strip()
        if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", ip):
            octets = ip.split(".")
            if all(0 <= int(octet) <= 255 for octet in octets):
                return ip
    return "Unable to determine"


def _run(cmd: list | str, check: bool = True, capture: bool = True, cwd: Optional[str] = None) -> Tuple[str, str]:
    """Run one legacy executor-setup shell command.

    Unlike the new GPU-splitting code, the executor bootstrap flow still uses
    shell strings for compatibility with the existing implementation. Typical
    commands include:

    - `git fetch --all`
    - `docker compose up -d`
    - `docker inspect --format=...`
    - `curl -4 -s ...`
    """
    import subprocess

    cmd_str = " ".join(cmd) if isinstance(cmd, list) else cmd
    result = subprocess.run(cmd_str, shell=True, cwd=cwd, text=True, capture_output=capture)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd_str}\n"
            f"--- stdout ---\n{(result.stdout or '')[:4000]}\n"
            f"--- stderr ---\n{(result.stderr or '')[:4000]}"
        )
    return (result.stdout or ""), (result.stderr or "")


def _exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _show_setup_summary():
    table = Table(title="Executor Setup Plan", show_header=False, box=box.SIMPLE_HEAVY)
    table.add_column("Step", style="cyan", no_wrap=True)
    table.add_column("What happens")
    table.add_row("1", "Clone or update compute-subnet repo")
    table.add_row("2", "Install executor dependencies")
    table.add_row("3", "Prerequisite check (Docker, NVIDIA GPU)")
    table.add_row("4", "Configure executor .env (ports, hotkey)")
    table.add_row("5", "Start executor with docker compose")
    table.add_row("6", "Validate executor configuration")
    console.print(table)
    console.print()


def _clone_or_update_repo(target_dir: Path, branch: str):
    """Ensure the compute-subnet repository exists at the desired branch.

    Commands run:

    - existing repo path:
      `git fetch --all`
      `git checkout <branch>`
      `git pull origin <branch>`
    - missing repo path:
      `git clone --branch <branch> https://github.com/Datura-ai/lium-io.git <target_dir>`
    """
    if target_dir.exists():
        if (target_dir / ".git").exists():
            _run("git fetch --all", cwd=str(target_dir))
            _run(f"git checkout {branch}", cwd=str(target_dir))
            _run(f"git pull origin {branch}", cwd=str(target_dir))
        return
    _run(f"git clone --branch {branch} https://github.com/Datura-ai/lium-io.git {target_dir}")


def _check_prereqs():
    """Verify local executor bootstrap prerequisites.

    Commands run:

    - `nvidia-smi --query-gpu=name --format=csv,noheader`
    - `docker info`

    Binary existence checks are also performed for:

    - `nvidia-smi`
    - `nvidia-container-cli`
    - `docker`
    """
    if not _exists("nvidia-smi"):
        raise Exception("NVIDIA GPU driver not found (nvidia-smi missing)")
    _run("nvidia-smi --query-gpu=name --format=csv,noheader")
    if not _exists("nvidia-container-cli"):
        raise Exception("NVIDIA Container Toolkit not found (required for Docker GPU access)")
    if not _exists("docker"):
        raise Exception("Docker not found")
    _run("docker info")


def _install_executor_tools(compute_dir: Path):
    """Run the repo-provided Ubuntu installer for executor dependencies.

    Command run:

    - `bash <compute_dir>/scripts/install_executor_on_ubuntu.sh`
    """
    script = compute_dir / "scripts" / "install_executor_on_ubuntu.sh"
    if not script.exists():
        raise Exception(f"Install script not found at {script}")
    _run(f"bash {script}")


def _setup_executor_env(
    executor_dir: str | Path,
    *,
    hotkey: str,
    internal_port: int = 8080,
    external_port: int = 8080,
    ssh_port: int = 2200,
    ssh_public_port: str = "",
    port_range: str = "",
):
    """Render `neurons/executor/.env` from `.env.template`.

    No shell commands run here. This is a pure file transform that updates the
    key bootstrap variables before Docker Compose starts the executor.
    """
    executor_dir = Path(executor_dir)
    env_t = executor_dir / ".env.template"
    env_f = executor_dir / ".env"

    if not env_t.exists():
        raise Exception(f"Template file not found at {env_t}")

    def _valid_port(p: int) -> bool:
        return isinstance(p, int) and 1 <= p <= 65535

    if not re.fullmatch(r"[1-9A-HJ-NP-Za-km-z]{40,60}", hotkey or ""):
        raise Exception(f"Invalid hotkey format: {hotkey}")

    for p, name in [(internal_port, "INTERNAL_PORT"), (external_port, "EXTERNAL_PORT"), (ssh_port, "SSH_PORT")]:
        if not _valid_port(p):
            raise Exception(f"Invalid port {name}={p} (must be 1-65535)")

    src_lines = env_t.read_text().splitlines()
    out_lines = []
    seen = set()

    def put(k: str, v: str | int):
        out_lines.append(f"{k}={v}")
        seen.add(k)

    for line in src_lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            out_lines.append(line)
            continue

        k, _ = line.split("=", 1)
        if k == "MINER_HOTKEY_SS58_ADDRESS":
            put(k, hotkey)
        elif k == "INTERNAL_PORT":
            put(k, internal_port)
        elif k == "EXTERNAL_PORT":
            put(k, external_port)
        elif k == "SSH_PORT":
            put(k, ssh_port)
        elif k == "SSH_PUBLIC_PORT":
            out_lines.append(line if not ssh_public_port else f"{k}={ssh_public_port}")
        elif k == "RENTING_PORT_RANGE":
            out_lines.append(line if not port_range else f"{k}={port_range}")
        else:
            out_lines.append(line)

    required = {
        "MINER_HOTKEY_SS58_ADDRESS": hotkey,
        "INTERNAL_PORT": internal_port,
        "EXTERNAL_PORT": external_port,
        "SSH_PORT": ssh_port,
    }
    for k, v in required.items():
        if k not in seen and not any(line.startswith(f"{k}=") for line in out_lines):
            out_lines.append(f"{k}={v}")

    env_f.write_text("\n".join(map(str, out_lines)) + "\n")


def _start_executor(executor_dir: Path, wait_secs: int = 180):
    """Start the executor stack and poll until the service is healthy.

    Commands run:

    - `docker compose up -d`
    - repeated `docker compose -f docker-compose.app.yml ps -q executor`
    - repeated `docker inspect --format='{{.State.Health.Status}}' <container_id>`
    """
    _run("docker compose up -d", capture=True, cwd=str(executor_dir))
    start = time.time()
    while time.time() - start < wait_secs:
        out, _ = _run("docker compose -f docker-compose.app.yml ps -q executor", cwd=str(executor_dir))
        if not out.strip():
            time.sleep(2)
            continue
        container_id = out.strip()
        out, _ = _run(f"docker inspect --format='{{{{.State.Health.Status}}}}' {container_id}")
        if out.strip() == "healthy":
            return
        time.sleep(3)
    raise Exception(f"Executor health check timed out after {wait_secs}s")


def _apply_env_overrides(executor_dir: Path, internal: str, external: str, ssh: str, ssh_pub: str, rng: str):
    """Apply CLI-specified port overrides to `.env`.

    No shell commands run here. This only rewrites the already-rendered env
    file after `_setup_executor_env(...)` has created it.
    """
    env_f = executor_dir / ".env"
    content = env_f.read_text().splitlines()

    def set_or_append(key, val):
        nonlocal content
        pat = f"{key}="
        for index, line in enumerate(content):
            if line.startswith(pat):
                content[index] = f"{pat}{val}"
                break
        else:
            content.append(f"{pat}{val}")

    set_or_append("INTERNAL_PORT", internal)
    set_or_append("EXTERNAL_PORT", external)
    set_or_append("SSH_PORT", ssh)
    if ssh_pub:
        set_or_append("SSH_PUBLIC_PORT", ssh_pub)
    if rng:
        set_or_append("RENTING_PORT_RANGE", rng)
    env_f.write_text("\n".join(content) + "\n")


def _gather_inputs(hotkey: Optional[str], auto: bool) -> dict:
    answers = {}
    if auto:
        answers["hotkey"] = hotkey or ""
        answers.update(
            dict(internal_port="8080", external_port="8080", ssh_port="2200", ssh_public_port="", port_range="")
        )
        return answers

    console.print("\n[bold]We're setting up how your executor can be reached.[/bold]\n")
    console.print("• [cyan]Service port[/cyan] -> where the executor's HTTP API listens (default 8080).")
    console.print("• [cyan]Executor SSH port[/cyan] -> used by validators to SSH into the container (default 2200).")
    console.print("• [cyan]Public SSH port[/cyan] -> only if your server is behind NAT and you forward a different public port.")
    console.print("• [cyan]Renting port range[/cyan] -> optional, used only if your firewall limits outbound ports.\n")

    if not hotkey:
        hotkey = Prompt.ask("Miner hotkey SS58 address")
    else:
        console.print(f"Miner hotkey SS58 address: [yellow]{hotkey}[/yellow]\n")
    answers["hotkey"] = hotkey or ""

    def ask_port(label, default):
        while True:
            value = Prompt.ask(label, default=str(default))
            if not value:
                return ""
            if value.isdigit() and 1 <= int(value) <= 65535:
                return value
            console.warning("Port must be an integer between 1 and 65535.")

    service_port = ask_port("Service port (where the executor API will be reachable)", 8080)
    answers["internal_port"] = service_port
    answers["external_port"] = service_port
    answers["ssh_port"] = ask_port("Executor SSH port (used by validator to SSH into the container)", 2200)

    ssh_public = Prompt.ask(
        "Public SSH port (optional, only if behind NAT and forwarding a different port)",
        default="",
    )
    answers["ssh_public_port"] = ssh_public if ssh_public and ssh_public.isdigit() else ""
    answers["port_range"] = Prompt.ask(
        "Renting port range (optional, e.g. 2000-2005 or 2000,2001). Leave empty if all ports open",
        default="",
    )
    return answers


def _validate_executor():
    """Run the validator container against the local GPU/Docker setup.

    Command run:

    - `docker run --rm --gpus all daturaai/lium-validator:latest`
    """
    out, _ = _run("docker run --rm --gpus all daturaai/lium-validator:latest", check=False)
    result = json.loads(out.strip())
    if not result.get("passed", False):
        raise Exception(result.get("message", "Validation failed"))


@click.command("executor-setup")
@click.option("--hotkey", "-k", help="Miner hotkey SS58 address")
@click.option("--dir", "-d", "dir_", default="compute-subnet", help="Target directory")
@click.option("--branch", "-b", default="main")
@click.option("--auto", "-a", is_flag=True)
@click.option("--verbose", "-v", is_flag=True, help="Show the plan banner")
@handle_errors
def executor_setup_command(hotkey, dir_, branch, auto, verbose):
    """Set up a compute subnet executor/miner."""
    if verbose:
        _show_setup_summary()

    answers = _gather_inputs(hotkey, auto)
    target_dir = Path(dir_).absolute()
    total_steps = 6

    try:
        with timed_step_status(1, total_steps, "Ensuring repository"):
            _clone_or_update_repo(target_dir, branch)
        with timed_step_status(2, total_steps, "Installing executor tools"):
            _install_executor_tools(target_dir)
        with timed_step_status(3, total_steps, "Checking prerequisites"):
            _check_prereqs()
        with timed_step_status(4, total_steps, "Configuring environment"):
            executor_dir = target_dir / "neurons" / "executor"
            if not executor_dir.exists():
                raise Exception(f"Executor directory not found at {executor_dir}")
            _setup_executor_env(str(executor_dir), hotkey=answers["hotkey"])
            _apply_env_overrides(
                executor_dir,
                internal=answers["internal_port"],
                external=answers["external_port"],
                ssh=answers["ssh_port"],
                ssh_pub=answers["ssh_public_port"],
                rng=answers["port_range"],
            )
        with timed_step_status(5, total_steps, "Starting executor"):
            _start_executor(executor_dir)
        with timed_step_status(6, total_steps, "Validating executor"):
            _validate_executor()
    except Exception as exc:
        console.error(f"❌ {exc}")
        return

    gpu_info = _get_gpu_info()
    public_ip = _get_public_ip()
    external_port = answers.get("external_port", "8080")

    console.success("\n✨ Executor setup complete!")
    console.print()

    details_table = Table(show_header=False, box=None)
    details_table.add_column("Key", style="cyan")
    details_table.add_column("Value", style="white")
    details_table.add_row("📍 Endpoint", f"{public_ip}:{external_port}")
    details_table.add_row("🎮 GPU", f"{gpu_info['gpu_count']}×{gpu_info['gpu_type']}")
    details_table.add_row("📂 Directory", str(executor_dir))
    hotkey_value = answers.get("hotkey", "Not set")
    details_table.add_row("🔑 Hotkey", hotkey_value[:20] + "..." if len(hotkey_value) > 20 else hotkey_value)
    console.print(Panel(details_table, title="[bold]Executor Details[/bold]", border_style="green"))

    from urllib.parse import urlencode

    add_url = (
        "https://provider.lium.io/executors?"
        + urlencode(
            {
                "action": "add",
                "gpu_type": gpu_info.get("gpu_type", "Unknown"),
                "ip_address": public_ip,
                "port": external_port,
                "gpu_count": gpu_info.get("gpu_count", 0),
            }
        )
    )
    console.print("\n[bold cyan]Add this executor via web interface:[/bold cyan]")
    console.print(f"[yellow]{add_url}[/yellow]\n")
