"""Mine command for setting up a compute subnet executor/miner."""

import json
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

import click
from rich import box
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from ..utils import console, handle_errors, timed_step_status

_SS58_HOTKEY = r"[1-9A-HJ-NP-Za-km-z]{40,60}"  # the shape `lium mine -k` validates and `mine status` refuses

if TYPE_CHECKING:
    from . import mine_register


# --------------------------
# Helpers
# --------------------------
def _get_gpu_info() -> dict:
    """Get GPU information using nvidia-smi."""
    # Query only the GPU name field to get clean output
    out, _ = _run("nvidia-smi --query-gpu=name --format=csv,noheader")
    lines = out.strip().split('\n')

    if lines and lines[0]:
        # Get the first GPU's name (all GPUs in a system are typically the same model)
        gpu_name = lines[0].strip()
        # Count the number of GPUs
        gpu_count = len(lines)
        return {"gpu_count": gpu_count, "gpu_type": gpu_name}
    
    return {"gpu_count": 0, "gpu_type": None}


def _get_public_ip() -> str:
    """Get the public IP address (IPv4 only)."""
    # Try multiple services for redundancy, requesting IPv4 explicitly
    services = [
        "https://api.ipify.org?format=text",
        "https://ipv4.icanhazip.com",
        "https://ifconfig.me/ip"
    ]

    for service in services:
        # check=False: a service that is down (curl exit 6/7/28) is skipped, the next one is asked
        out, _ = _run(f"curl -4 -s {service}", check=False)
        ip = out.strip()
        # Validate IPv4 format (strict check for valid octets)
        if re.match(r"^(?:[0-9]{1,3}\.){3}[0-9]{1,3}$", ip):
            # Additional check: each octet must be 0-255
            octets = ip.split('.')
            if all(0 <= int(octet) <= 255 for octet in octets):
                return ip
    return "Unable to determine"

def _subprocess_env() -> dict:
    """The environment for the tools `lium mine` shells out to.

    The Linux binary is a PyInstaller bundle: it puts its own ``_internal/`` (an older libstdc++) on
    ``LD_LIBRARY_PATH`` and keeps the caller's value in ``LD_LIBRARY_PATH_ORIG``. Children such as
    ``apt-get`` then load that libstdc++ and die with ``GLIBCXX_3.4.32 not found`` — the install
    script's ``apt update failed after 5 attempts`` on Ubuntu 22.04/24.04. Give them the host's.
    """
    import os
    import sys

    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        orig = env.pop("LD_LIBRARY_PATH_ORIG", None)
        env.pop("LD_LIBRARY_PATH", None)
        if orig:
            env["LD_LIBRARY_PATH"] = orig
    return env


def _run(cmd: list | str, check=True, capture=True, cwd: Optional[str] = None) -> Tuple[str, str]:
    import subprocess
    if isinstance(cmd, list):
        cmd_str = " ".join(cmd)
    else:
        cmd_str = cmd
    result = subprocess.run(
        cmd_str,
        shell=True,
        cwd=cwd,
        text=True,
        capture_output=capture,
        env=_subprocess_env(),
    )
    if check and result.returncode != 0:
        # the cause of a failed step is the LAST thing a tool prints; the first 4000 chars of a
        # `docker compose up` are image-pull progress and the error is cut off
        raise RuntimeError(
            f"Command failed ({result.returncode}): {cmd_str}\n"
            # DAH-3075/3076: the tail — compose puts the error after screens of pull progress
            f"--- stdout ---\n{(result.stdout or '')[-4000:]}\n"
            f"--- stderr ---\n{(result.stderr or '')[-4000:]}"
        )
    return (result.stdout or ""), (result.stderr or "")


def _exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _show_setup_summary(register: bool = False):
    table = Table(title="Node Setup Plan", show_header=False, box=box.SIMPLE_HEAVY)
    table.add_column("Step", style="cyan", no_wrap=True)
    table.add_column("What happens")
    table.add_row("1", "Clone or update compute-subnet repo")
    table.add_row("2", "Install node dependencies")
    table.add_row("3", "Prerequisite check (Docker, NVIDIA GPU)")
    table.add_row("4", "Configure node .env (ports, hotkey)")
    table.add_row("5", "Start node with docker compose")
    table.add_row("6", "Validate node configuration")
    if register:
        table.add_row("7", "Register the node in the portal (--register)")
        table.add_row("8", "Wait until the portal lists it")
    console.print(table)
    console.print()


# --------------------------
# Actions
# --------------------------
def _clone_or_update_repo(target_dir: Path, branch: str):
    if target_dir.exists():
        if (target_dir / ".git").exists():
            _run("git fetch --all", cwd=str(target_dir))
            _run(f"git checkout {branch}", cwd=str(target_dir))
            _run(f"git pull origin {branch}", cwd=str(target_dir))

    else:
        _run(f"git clone --branch {branch} https://github.com/Datura-ai/lium-io.git {target_dir}")


def _check_prereqs():
    if not _exists("nvidia-smi"):
        raise Exception("NVIDIA GPU driver not found (nvidia-smi missing)")

    _run("nvidia-smi --query-gpu=name --format=csv,noheader")

    if not _exists("nvidia-container-cli"):
        raise Exception("NVIDIA Container Toolkit not found (required for Docker GPU access)")

    if not _exists("docker"):
        raise Exception("Docker not found")

    _run("docker info")


def _install_executor_tools(compute_dir: Path):
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
    """
    Render neurons/executor/.env from .env.template with provided values.

    - Never prompts.
    - Preserves unknown lines/keys from the template.
    - Ensures required keys exist even if missing in template.
    """
    executor_dir = Path(executor_dir)
    env_t = executor_dir / ".env.template"
    env_f = executor_dir / ".env"

    if not env_t.exists():
        raise Exception(f"Template file not found at {env_t}")

    # light sanity checks (don't be strict)
    def _valid_port(p: int) -> bool:
        return isinstance(p, int) and 1 <= p <= 65535

    if not re.fullmatch(_SS58_HOTKEY, hotkey or ""):
        raise Exception(f"Invalid hotkey format: {hotkey}")

    for p, name in [(internal_port, "INTERNAL_PORT"),
                    (external_port, "EXTERNAL_PORT"),
                    (ssh_port, "SSH_PORT")]:
        if not _valid_port(p):
            raise Exception(f"Invalid port {name}={p} (must be 1-65535)")

    # read, rewrite, preserve
    src_lines = env_t.read_text().splitlines()
    out_lines = []
    seen = set()

    def put(k: str, v: str | int):
        nonlocal out_lines, seen
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
            # DAH-3075: blank means "same as SSH_PORT" (the documented meaning); keeping the template's
            # hard-coded 2200 while SSH_PORT changed sent the validator to a port nothing listens on
            put(k, ssh_public_port or ssh_port)
        elif k == "RENTING_PORT_RANGE":
            if port_range:
                put(k, port_range)
            else:
                out_lines.append(line)
        else:
            out_lines.append(line)  # unknown key: preserve

    # ensure required keys exist even if template lacked them
    required = {
        "MINER_HOTKEY_SS58_ADDRESS": hotkey,
        "INTERNAL_PORT": internal_port,
        "EXTERNAL_PORT": external_port,
        "SSH_PORT": ssh_port,
    }
    for k, v in required.items():
        if k not in seen and not any(l.startswith(f"{k}=") for l in out_lines):
            out_lines.append(f"{k}={v}")

    env_f.write_text("\n".join(map(str, out_lines)) + "\n")


def _listening_process(port: int) -> str:
    """Best-effort 'who owns this port' hint from ``ss -ltnp`` (Linux only)."""
    if not _exists("ss"):
        return ""
    out, _ = _run("ss -ltnp", check=False)
    # Without root, ss hides the owner of other users' sockets (e.g. docker-proxy).
    if "users:" not in out and _exists("sudo"):
        sudo_out, _ = _run("sudo -n ss -ltnp", check=False)
        out = sudo_out or out
    for line in out.splitlines():
        if re.search(rf"[:\]]{port}\s", line):
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            if m:
                return f"{m.group(1)} pid {m.group(2)}"
            return "unknown process"
    return ""


def _port_in_use(port: int, host: str = "0.0.0.0") -> bool:
    """True when ``docker compose up`` could not bind ``<host>:<port>`` on this host.

    This is a probe, not a listener: the socket is bound for an instant,
    never ``listen()``-ed, and closed on return. ``host`` defaults to the
    wildcard address because the executor's ``docker-compose.app.yml``
    publishes ``EXTERNAL_PORT`` and ``SSH_PORT`` on every interface, so the
    probe must fail exactly where ``docker compose up`` would: a wildcard
    bind fails when a listener holds the port on any single interface, which
    a ``127.0.0.1`` probe would miss.

    On Linux the probe binds with ``SO_REUSEADDR``, as docker-proxy's own bind
    does (Go sets it on every listener): a port whose only sockets are in
    ``TIME_WAIT`` left by a socket that had the option itself — the executor's
    own connections for up to 60 s after ``docker compose down``, the case a
    re-run hits — is free again, while a ``LISTEN`` socket on any address still
    refuses the bind (Linux ignores ``TIME_WAIT`` only when both sockets set the
    option, and never a listener). Without the option the plain bind refused
    ``TIME_WAIT`` too and reported a port nobody held. BSD/macOS reads the option
    differently (a wildcard bind may sit next to a specific-address listener),
    so there the plain bind stays; ``lium mine`` brings up a Linux host. IPv4
    only: the executor's compose publishes ``0.0.0.0``; an IPv6-only listener on
    the port is not seen here (compose would then fail at ``:::<port>``). Only
    ``EADDRINUSE`` means taken: ``EACCES`` on a privileged port from an
    unprivileged ``lium mine`` is left to compose, which binds as root.
    """
    import errno
    import socket
    import sys

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform.startswith("linux"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError as e:
            # only "somebody holds it" is a conflict: EACCES on a port below 1024 says this uid may not bind it,
            # which compose (root) can — a provider running `lium mine` unprivileged must not be told 443 is taken
            return e.errno == errno.EADDRINUSE
    return False


def _host_ports_from_answers(answers: dict) -> dict[str, int]:
    """Host ports docker compose will publish, from the gathered answers.

    The public SSH port is only a NAT forward on the provider's router, so
    it is not bound on this host and is not checked.
    """
    ports: dict[str, int] = {}
    for label, key in (("service port", "external_port"), ("SSH port", "ssh_port")):
        v = str(answers.get(key) or "").strip()
        if v.isdigit():
            ports[label] = int(v)
    return ports


def _compose_project_running(executor_dir: Path) -> bool:
    """Whether this executor's own ``executor`` service is in the ``running`` state.

    On a re-run (`lium mine` on a host whose executor is up: the update path) the ports are
    held by the project's own ``docker-proxy``; ``docker compose up -d`` is then a no-op, so
    the pre-check must not fail on our own listener. The question is asked of the ``executor``
    service alone (``docker-compose.app.yml``, the file the health wait reads too): the project's
    ``watchtower`` sidecar is always running, and an executor whose port bind failed ("address
    already in use" — the case this check exists for) is left ``created``, not ``running`` (its
    start never succeeded, so no restart policy applies; the runner is what restarts), so the
    ports are then checked as on a first run.
    """
    try:
        out, _ = _run("docker compose -f docker-compose.app.yml ps -q --status running executor",
                      check=False, cwd=str(executor_dir))
    except OSError:   # no such directory yet (first run): nothing of ours is running
        return False
    return bool(out.strip())


def _check_ports_free(ports: dict[str, int], executor_dir: Optional[Path] = None) -> None:
    """Fail before ``docker compose up`` when a configured host port is taken.

    ``ports`` maps a human label to the port number, e.g.
    ``{"service port": 8080, "SSH port": 2200}``. Without this check the
    executor container enters a restart loop and the caller only sees the
    health check time out three minutes later. Skipped when the executor's
    own ``executor`` service is already running (``executor_dir`` given):
    those listeners are ours and ``compose up`` keeps them.
    """
    if executor_dir is not None and _compose_project_running(executor_dir):
        return
    for label, port in ports.items():
        if not port or not _port_in_use(port):
            continue
        owner = _listening_process(port)
        who = f" ({owner})" if owner else ""
        raise Exception(
            f"Port {port} ({label}) is already in use on this host{who}. "
            "Free it or pick another port (run `lium mine` without --auto to choose ports)."
        )


# the executor project is two compose files: the default one declares `executor-runner` (and `watchtower`),
# `docker-compose.app.yml` declares `executor`, which the runner starts. Compose checks a named service against
# the file it loaded (`no such service` otherwise), so each file is asked for its own service.
_COMPOSE_SERVICES = (("", "executor-runner"), ("-f docker-compose.app.yml ", "executor"))


def _compose_diagnostics(executor_dir: Path, tail: int = 30) -> str:
    """``docker compose ps -a`` of both files + the last log lines of ``executor-runner`` and ``executor``.

    ``-a`` because an executor whose port bind failed is left ``created`` (its start never
    succeeded, so no restart policy applies) and plain ``ps`` would not list it. Used when the
    health check times out so the actual failure (port conflict, image pull error, bad .env) is
    on screen instead of only 'timed out'.
    """
    parts = []
    for file_opt, _service in _COMPOSE_SERVICES:
        ps, _ = _run(f"docker compose {file_opt}ps -a", check=False, cwd=str(executor_dir))
        if ps.strip():
            parts.append(f"--- docker compose {file_opt}ps -a ---\n" + ps.strip())
    for file_opt, service in _COMPOSE_SERVICES:
        logs, err = _run(
            f"docker compose {file_opt}logs --no-color --tail {tail} {service}",
            check=False,
            cwd=str(executor_dir),
        )
        text = (logs or "") + (err or "")
        if text.strip():
            parts.append(f"--- last {tail} log lines ({service}) ---\n" + text.strip()[-4000:])
    return "\n".join(parts)


def _start_executor(executor_dir: Path, wait_secs: int = 180):
    # Start using the default docker-compose.yml
    _run("docker compose up -d", capture=True, cwd=str(executor_dir))

    # Wait for the executor service to be fully healthy
    start = time.time()
    
    while time.time() - start < wait_secs:
        # Get the container name/ID for the executor service
        out, _ = _run("docker compose -f docker-compose.app.yml ps -q executor", cwd=str(executor_dir))
        if not out.strip():
            time.sleep(2)
            continue
            
        container_id = out.strip()
        
        # Check the health status directly using docker inspect
        out, _ = _run(f"docker inspect --format='{{{{.State.Health.Status}}}}' {container_id}")
        
        if out.strip():
            health_status = out.strip()
            # Only return true if explicitly healthy
            if health_status == "healthy":
                return
        time.sleep(3)
    diag = _compose_diagnostics(executor_dir)
    raise Exception(
        f"Node health check timed out after {wait_secs}s."
        + (f"\n{diag}" if diag else "")
    )

def _apply_env_overrides(
    executor_dir: Path,
    internal: str, external: str, ssh: str, ssh_pub: str, rng: str
):
    env_f = executor_dir / ".env"
    content = env_f.read_text().splitlines()
    def set_or_append(key, val):
        nonlocal content
        pat = f"{key}="
        for i, line in enumerate(content):
            if line.startswith(pat):
                content[i] = f"{pat}{val}"
                break
        else:
            content.append(f"{pat}{val}")
    set_or_append("INTERNAL_PORT", internal)
    set_or_append("EXTERNAL_PORT", external)
    set_or_append("SSH_PORT", ssh)
    # the executor advertises SSH_PUBLIC_PORT or SSH_PORT (miner_service.py); the template ships
    # SSH_PUBLIC_PORT=2200, so a blank answer must not leave 2200 next to a different SSH_PORT
    set_or_append("SSH_PUBLIC_PORT", ssh_pub or ssh)
    if rng:
        set_or_append("RENTING_PORT_RANGE", rng)
    env_f.write_text("\n".join(content) + "\n")

def _gather_inputs(
    hotkey: Optional[str],
    auto: bool,
) -> dict:
    """Ask everything up-front; return a dict of resolved inputs."""
    answers = {}
    if auto:
        # Auto mode - use all defaults
        answers["hotkey"] = hotkey or ""
        answers.update(dict(
            internal_port="8080",
            external_port="8080",
            ssh_port="2200",
            ssh_public_port="",
            port_range=""
        ))
    else:
        # Show informative header about port configuration
        console.print("\n[bold]We're setting up how your node can be reached.[/bold]\n")
        console.print("• [cyan]Service port[/cyan] → where the node's HTTP API listens (default 8080).")
        console.print("• [cyan]Node SSH port[/cyan] → used by validators to SSH into the container (default 2200).")
        console.print("• [cyan]Public SSH port[/cyan] → only if your server is behind NAT and you forward a different public port.")
        console.print("• [cyan]Renting port range[/cyan] → optional, used only if your firewall limits outbound ports.\n")
        
        if not hotkey:
            hotkey = Prompt.ask("Miner hotkey SS58 address")
        else:
            console.print(f"Miner hotkey SS58 address: [yellow]{hotkey}[/yellow]\n")
        answers["hotkey"] = hotkey or ""

        def ask_port(label, default):
            while True:
                v = Prompt.ask(label, default=str(default))
                if not v:  # Allow empty for optional ports
                    return ""
                if v.isdigit() and 1 <= int(v) <= 65535:
                    return v
                console.warning("Port must be an integer between 1 and 65535.")
        
        # Service ports
        service_port = ask_port("Service port (where the node API will be reachable)", 8080)
        answers["internal_port"] = service_port
        answers["external_port"] = service_port  # Set external same as internal
        answers["ssh_port"] = ask_port("Node SSH port (used by validator to SSH into the container)", 2200)
        
        # Optional ports
        ssh_public = Prompt.ask("Public SSH port (optional, only if behind NAT and forwarding a different port)", default="")
        answers["ssh_public_port"] = ssh_public if ssh_public and ssh_public.isdigit() else ""
        
        answers["port_range"] = Prompt.ask("Renting port range (optional, e.g. 2000-2005 or 2000,2001). Leave empty if all ports open", default="")

    return answers


PREFLIGHT_IMAGE = "daturaai/lium-validator:latest"


class _StepMessage:
    """Step label the spinner re-reads on every redraw, so a detail can change while it runs."""

    def __init__(self, text: str):
        self.text = text
        self.detail = ""

    def __str__(self) -> str:
        return f"{self.text} ({self.detail})" if self.detail else self.text


def _start_preflight_pull():
    """Pull the preflight image in the background.

    The image is ~900 MB (30–60 s on a typical provider link). Started right after the
    prerequisites pass, the pull overlaps steps 4–5 (env, compose up, health wait) instead
    of being paid inside "Validating node".
    """
    import subprocess

    return subprocess.Popen(
        f"docker pull {PREFLIGHT_IMAGE}",
        shell=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_subprocess_env(),
    )


def _validate_executor(extra_args=None, on_check=None):
    """Run the validator's preflight image and raise on a failed verdict.

    ``on_check(name)`` is called as each check starts (GPU configuration, matrix
    work-proof, VerifyX), read from the image's ``--debug`` log on stderr; the JSON
    verdict stays on stdout.
    """
    import subprocess

    docker_cmd = f"docker run --rm --gpus all {PREFLIGHT_IMAGE} --debug"
    if extra_args:
        docker_cmd += " " + " ".join(extra_args)

    # errors="replace": in --debug mode the matrix check echoes its raw cipher bytes
    # on stdout ahead of the JSON verdict.
    proc = subprocess.Popen(
        docker_cmd,
        shell=True,
        text=True,
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_subprocess_env(),
    )
    # Both pipes are drained at once: stdout on a thread, stderr here. Reading stderr to EOF
    # first would deadlock once the --debug stdout (cipher bytes ahead of the verdict) filled
    # the 64 KiB pipe — the child blocks on write, stderr never closes.
    import threading

    captured: dict[str, str] = {}

    def _drain_stdout() -> None:
        captured["out"] = proc.stdout.read()

    reader = threading.Thread(target=_drain_stdout, daemon=True)
    reader.start()
    err_tail: list[str] = []
    for line in proc.stderr:
        err_tail = (err_tail + [line.rstrip()])[-20:]
        m = re.search(r"Running check: (.+)$", line)
        if m and on_check:
            on_check(m.group(1).strip())
    reader.join()
    out = captured.get("out", "")
    proc.wait()

    result = _preflight_verdict(out)
    if result is None:
        raise Exception(
            "Preflight image produced no verdict (exit %s):\n%s"
            % (proc.returncode, "\n".join(err_tail)[-2000:])
        )
    if not result.get("passed", False):
        raise Exception(result.get("message", ""))


def _preflight_verdict(stdout: str) -> Optional[dict]:
    """The image's JSON verdict: the last object that starts at a line beginning on stdout."""
    for m in reversed(list(re.finditer(r"^\{", stdout, re.M))):
        try:
            return json.loads(stdout[m.start():])
        except ValueError:
            continue
    return None


_MINE_STATUS_PROG = "lium mine status"


def _mine_status(args: list[str], hotkey: Optional[str] = None) -> int:
    """Run `lium provider node status <args>`; `--json` may come after the node id here.

    ``hotkey`` is what `--hotkey`/`-k` was set to on the `lium mine` line: click gives it to `mine` wherever
    it is typed, so for `status` it is passed on as the provider group's `--hotkey` (the wallet hotkey name
    the portal is signed in with) — the flag the ARG_INVALID message asks for then means what it says.

    Help and usage errors are rendered under the name the user typed: click would otherwise compose
    ``lium mine status node status`` from the provider group's path.
    """
    from lium.cli.provider.command import provider_command
    from lium.cli.provider.node import status_node

    own_ctx = click.Context(status_node, info_name=_MINE_STATUS_PROG)
    if "--help" in args:
        # the leaf's help, plus the one option `mine` takes on its behalf (the changelog and the ARG_INVALID
        # message both name it, so `--help` has to as well)
        click.echo(status_node.get_help(own_ctx))
        click.echo(f"  {'--hotkey, -k NAME':<24}  Provider wallet hotkey the portal is signed in with (else")
        click.echo(f"  {'':<24}  LIUM_PROVIDER_HOTKEY / ~/.lium/config.ini).")
        return 0
    group_args = ["--json"] if "--json" in args else []
    if hotkey and re.fullmatch(_SS58_HOTKEY, hotkey):
        # `lium mine -k` takes the miner's ss58; here the same flag is the wallet hotkey NAME the portal is
        # signed in with. An ss58 would find no local wallet and end in PORTAL_AUTH_INVALID with no word
        # about the flag — say so before any request goes out.
        from lium.cli.provider._render import emit_error
        from lium.provider.errors import ARG_INVALID, ProviderError

        own_ctx.obj = {"provider_opts": {"json": bool(group_args)}}
        return emit_error(own_ctx, ProviderError(
            "--hotkey for 'lium mine status' is the wallet hotkey name the portal is signed in with "
            "(as for 'lium provider -k'), not the SS58 address 'lium mine' takes",
            code=ARG_INVALID,
            hint="Re-run with the wallet hotkey name, or set LIUM_PROVIDER_HOTKEY.",
        ))
    if hotkey:
        group_args += ["--hotkey", hotkey]
    sub_args = [a for a in args if a != "--json"]
    try:
        code = provider_command.main(
            args=[*group_args, "node", "status", *sub_args],
            prog_name=_MINE_STATUS_PROG,
            standalone_mode=False,
        )
    except click.UsageError as e:   # a missing node id, an unknown option: usage under our own name
        click.echo(f"{own_ctx.get_usage()}\nTry '{_MINE_STATUS_PROG} --help' for help.\n\nError: {e.format_message()}", err=True)
        return e.exit_code
    except click.ClickException as e:
        e.show()
        return e.exit_code
    return int(code or 0)


# --------------------------
# CLI
# --------------------------
@click.command("mine", context_settings=dict(ignore_unknown_options=True, allow_extra_args=True), add_help_option=False)
@click.option("--hotkey", "-k", help="Miner hotkey SS58 address (for `mine status`: the wallet hotkey name)")
@click.option("--dir", "-d", "dir_", default="compute-subnet", help="Target directory")
@click.option("--branch", "-b", default="main")
@click.option("--auto", "-a", is_flag=True)
@click.option("--verbose", "-v", is_flag=True, help="Show the plan banner")
# not click's eager --help: `lium mine status --help` must reach the status command below, not this one's help
@click.option("--help", "help_", is_flag=True, help="Show this message and exit.")
@click.option(
    "--register",
    "register_token",
    metavar="TOKEN",
    help="Register token from the portal's Add Node page: after the node is up, add it to your account and "
    "wait until it is listed. Implies --auto; the account comes from the token, so -k is not needed.",
)
@click.option(
    "--portal-url",
    envvar="LIUM_PORTAL_URL",
    default=None,
    help="Provider portal API (default: the production portal). Only with --register.",
)
@click.option(
    "--price",
    type=float,
    default=None,
    help="USD per GPU per hour for the registered node (default: the model's base price from lium.io's public "
    "shared-config; LIUM_SHARED_CONFIG_URL overrides the source). Only with --register.",
)
@click.option(
    "--gpu-type",
    default=None,
    help="Register under this portal GPU name instead of the one nvidia-smi reports (for when the portal "
    "does not know the reported name). Only with --register.",
)
@click.option(
    "--wait",
    "wait_minutes",
    type=click.IntRange(0, 24 * 60),
    default=45,
    show_default=True,
    help="Minutes to wait for the node to be listed after registering; 0 returns right after the add. "
    "Only with --register.",
)
@click.pass_context
@handle_errors
def mine_command(ctx, hotkey, dir_, branch, auto, verbose, help_, register_token, portal_url, price, gpu_type, wait_minutes):
    """Set up this host as a Lium provider node: clone, configure, start and validate the executor.

    Before `docker compose up`, the service and SSH ports are checked on this host: a port
    another process holds fails fast, naming that process, instead of a three-minute health
    timeout. On a host whose executor service is already running the check is skipped (the
    ports are ours). The preflight image is pulled while the node starts; its checks are shown
    as they run. Exit 1 on any failed step, with the step and the reason.

    With --register TOKEN (the command the portal's Add Node page shows) the account and the value the
    node reports under come from the token (an account created with e-mail or Google has no key of its own;
    the portal's is written to the executor's .env) and two steps follow: the node
    is added to your account with the GPU model and count nvidia-smi reports, this host's public
    IPv4 and the executor's port, at the model's base price from lium.io's public shared-config; then the node's
    status is polled every 15 s until it is listed (exit 0), the portal names something to fix
    (OFFLINE or VALIDATION_FAILED, exit 1), or --wait minutes pass (exit 2). The node page URL is
    printed in every case.
    """
    if ctx.args and ctx.args[0] == "status":
        # `lium mine` is the provider's first command; `lium mine status <node>` is where they look
        # next, so it is the same command as `lium provider node status` (auth from `--hotkey`/`-k`
        # anywhere on the line, else LIUM_PROVIDER_HOTKEY / ~/.lium/config.ini). Extra args are
        # otherwise the validator's.
        raise SystemExit(_mine_status(ctx.args[1:] + (["--help"] if help_ else []), hotkey=hotkey))
    if help_:
        click.echo(ctx.get_help())
        raise SystemExit(0)   # not ctx.exit(): handle_errors would report click's Exit as an unexpected error
    from . import mine_register as reg

    if verbose:
        _show_setup_summary(register=bool(register_token))   # keep the banner only when asked

    token: Optional[reg.RegisterToken] = None
    if not register_token:
        # typed on the command line only: LIUM_PORTAL_URL in the environment (the `lium provider` group's
        # variable) must not turn a plain `lium mine` into a usage error
        from click.core import ParameterSource

        given = [flag for flag, param in (("--portal-url", "portal_url"), ("--price", "price"),
                                          ("--gpu-type", "gpu_type"), ("--wait", "wait_minutes"))
                 if ctx.get_parameter_source(param) == ParameterSource.COMMANDLINE]
        if given:
            raise click.UsageError(f"{', '.join(given)}: only with --register TOKEN.")
    if register_token:
        # fail on a bad or expired token before the ten-minute install, not after it
        try:
            token = reg.parse_register_token(register_token)
        except reg.RegisterError as e:
            from rich.markup import escape

            console.error(f"❌ {escape(str(e))}")
            raise SystemExit(1)
        if hotkey and hotkey != token.node_hotkey:
            console.error(
                "❌ --hotkey differs from what the register token says this node reports under; drop -k, the token decides."
            )
            raise SystemExit(1)
        # what the executor reports under: the account's own key, or the portal's for an account without one
        # (lium-platform#294) — the SS58 check in _setup_executor_env applies to this value, not to the account id
        hotkey = token.node_hotkey
        auto = True
        left = token.seconds_left()
        if left is not None and left < 15 * 60:
            console.warning(
                f"The register token expires in {left // 60} min; the install alone takes 5–10 min. "
                "If registration fails with an expired token, copy a fresh command from the portal."
            )

    answers = _gather_inputs(hotkey, auto)
    target_dir = Path(dir_).absolute()

    TOTAL_STEPS = 8 if token else 6

    try:
        with timed_step_status(1, TOTAL_STEPS, "Ensuring repository"):
            _clone_or_update_repo(target_dir, branch)

        with timed_step_status(2, TOTAL_STEPS, "Installing node tools"):
            _install_executor_tools(target_dir)

        with timed_step_status(3, TOTAL_STEPS, "Checking prerequisites"):
            _check_prereqs()

        # Docker is confirmed; fetch the preflight image while steps 4–5 run.
        preflight_pull = _start_preflight_pull()

        with timed_step_status(4, TOTAL_STEPS, "Configuring environment"):
            executor_dir = target_dir / "neurons" / "executor"
            if not executor_dir.exists():
                raise Exception(f"Node directory not found at {executor_dir}")

            _setup_executor_env(
                str(executor_dir),
                hotkey=answers["hotkey"],
            )

            _apply_env_overrides(
                executor_dir,
                internal=answers["internal_port"],
                external=answers["external_port"],
                ssh=answers["ssh_port"],
                ssh_pub=answers["ssh_public_port"],
                rng=answers["port_range"],
            )

            # A taken host port makes `docker compose up` loop on
            # "address already in use" and the health check below time out
            # with no explanation. Catch it here, before the 3-minute wait.
            _check_ports_free(_host_ports_from_answers(answers), executor_dir)

        with timed_step_status(5, TOTAL_STEPS, "Starting node"):
            _start_executor(executor_dir)

        console.dim(
            "Validation runs the validator's preflight image: GPU check, matrix "
            "work-proof and VerifyX (RAM, disk throughput, network). Typically 2–4 minutes."
        )
        step6 = _StepMessage("Validating node")
        with timed_step_status(6, TOTAL_STEPS, step6):
            if preflight_pull.poll() is None:
                step6.detail = "pulling preflight image"
                preflight_pull.wait()

            def _show_check(name: str) -> None:
                step6.detail = name

            # Pass any extra arguments to the validator
            _validate_executor(ctx.args if ctx.args else None, on_check=_show_check)

    except Exception as e:
        # the message carries tool output verbatim (compose `ps -a`, log tails, a stderr tail): escaped, or a
        # `[type=…]` / `[/x]` token in it is Rich markup — eaten, or a MarkupError in place of the diagnosis
        from rich.markup import escape

        console.error(f"❌ {escape(str(e))}")
        raise SystemExit(1)   # a failed step is a failed command: mine.sh and scripts read the exit code

    if token:
        raise SystemExit(_register_and_wait(
            token,
            executor_dir=executor_dir,
            portal_url=portal_url,
            price=price,
            gpu_type_override=gpu_type,
            wait_minutes=wait_minutes,
            total_steps=TOTAL_STEPS,
        ))

    # Get executor details for summary
    gpu_info = _get_gpu_info()
    public_ip = _get_public_ip()
    
    # Get the external port from answers
    external_port = answers.get("external_port", "8080")
    
    console.success("\n✨ Node setup complete!")
    console.print()
    
    details_table = Table(show_header=False, box=None)
    details_table.add_column("Key", style="cyan")
    details_table.add_column("Value", style="white")
    
    details_table.add_row("📍 Endpoint", f"{public_ip}:{external_port}")
    details_table.add_row("🎮 GPU", f"{gpu_info['gpu_count']}×{gpu_info['gpu_type']}")
    details_table.add_row("📂 Directory", str(executor_dir))
    details_table.add_row("🔑 Hotkey", answers.get("hotkey", "Not set")[:20] + "..." if len(answers.get("hotkey", "")) > 20 else answers.get("hotkey", "Not set"))
    
    console.print(Panel(details_table, title="[bold]Node Details[/bold]", border_style="green"))
    
    # Generate URL for adding executor via web interface
    from urllib.parse import urlencode
    
    # Build query parameters
    params = {
        'action': 'add',
        'gpu_type': gpu_info.get('gpu_type', 'Unknown'),
        'ip_address': public_ip,
        'port': external_port,
        'gpu_count': gpu_info.get('gpu_count', 0)
    }
    
    # Build full URL with proper encoding
    add_url = f"https://provider.lium.io/nodes?{urlencode(params)}"
    
    console.print("\n[bold cyan]Register this node in the Provider Portal:[/bold cyan]")
    console.print(f"[yellow]{add_url}[/yellow]\n")
    console.print("[bold cyan]…or from this terminal:[/bold cyan]")
    console.print(f"[yellow]{_provider_add_command(gpu_info, public_ip, external_port)}[/yellow]")
    console.dim(_registration_note())


def _register_and_wait(
    token: "mine_register.RegisterToken",
    *,
    executor_dir: Path,
    portal_url: Optional[str],
    price: Optional[float],
    gpu_type_override: Optional[str],
    wait_minutes: int,
    total_steps: int,
) -> int:
    """Steps 7–8 of `lium mine --register`: add the node to the account, then watch its status. Returns the exit code."""
    from rich.markup import escape

    from . import mine_register as reg

    http = reg.build_http(portal_url, token.token)
    node_url = f"{reg.portal_web_url(portal_url)}/nodes"
    try:
        with timed_step_status(7, total_steps, "Registering node in the portal"):
            inventory = reg.read_gpu_inventory(_run)
            gpu_type = gpu_type_override or inventory.gpu_type
            port = reg.executor_port(executor_dir)
            ip = reg.public_ipv4_or_fail(_get_public_ip())
            price_per_gpu = reg.resolve_price(gpu_type, price)
            record = reg.register_node(
                http,
                miner_hotkey=token.miner_hotkey,
                gpu_type=gpu_type,
                gpu_count=inventory.gpu_count,
                ip_address=ip,
                port=port,
                price_per_gpu=price_per_gpu,
            )
    except Exception as e:   # RegisterError, a portal error, nvidia-smi exiting non-zero, an unreadable .env: same shape as steps 1–6
        console.error(f"❌ {escape(str(e))}")
        return 1

    if record.node_id is None:
        # the add went through; the list did not show it within ~30 s — nothing to poll, nothing failed, but the node
        # is registered and not listed, which docs/exit-codes.md says is exit 2
        console.success(escape(
            f"\n✨ Node added: {inventory.gpu_count}×{gpu_type} ({inventory.vram_gb} GB) at {ip}:{port}, "
            f"${price_per_gpu:g}/GPU/h — it is not in the node list yet; the page shows it when it is: {node_url}"
        ))
        return 2

    node_url = f"{node_url}/{record.node_id}"
    if record.already_registered:
        # the portal's record, not this run's values, is what stands: name only the address
        console.success(escape(f"\n✨ Node already in the portal at {ip}:{port}"))
    else:
        console.success(escape(
            f"\n✨ Node added: {inventory.gpu_count}×{gpu_type} ({inventory.vram_gb} GB) at {ip}:{port}, "
            f"${price_per_gpu:g}/GPU/h"
        ))
    console.print(f"[yellow]{escape(node_url)}[/yellow]")
    fix = reg.opt_in_fix(token, portal_url)
    if fix:
        console.warning(escape(fix))
    if wait_minutes == 0:
        return 0

    console.print(escape(f"\n● [8/{total_steps}] Waiting for the validator (up to {wait_minutes} min; Ctrl-C leaves the node registered)"))
    started = time.monotonic()
    try:
        final = reg.wait_until_listed(
            http,
            record.node_id,
            timeout_s=wait_minutes * 60,
            on_change=lambda s, t: console.print(escape(reg.status_line(s, t))),
        )
    except KeyboardInterrupt:
        console.print(escape(f"\nStopped watching; the node stays registered: {node_url}"))
        return 2
    message, code = reg.result_summary(final, node_url=node_url, waited_s=time.monotonic() - started)
    if code == 0:
        console.success(message)
    elif code == 1:
        console.error(escape(message))
    else:
        console.warning(escape(message))
    return code


def _provider_add_command(gpu_info: dict, public_ip: str, external_port: str | int) -> str:
    """The `lium provider node add` equivalent of the portal Add-Node modal."""
    import shlex

    gpu_type = gpu_info.get("gpu_type") or "Unknown"
    # _get_public_ip() answers "Unable to determine" when every IP service failed: printed bare that is three
    # extra argv words, so the placeholder says what to fill in instead
    ip = public_ip if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", public_ip or "") else "<public IPv4>"
    return (
        "lium provider node add "
        f"--gpu-type {shlex.quote(gpu_type)} --gpu-count {gpu_info.get('gpu_count', 0)} "
        f"--ip {ip} --port {external_port} --yes"
    )


def _registration_note() -> str:
    return (
        "Validators only reach nodes of providers with a running coordinator: opt in to the "
        "Lium Central Provider Server (`lium provider config opt-in --yes`, or Profile Settings "
        "in the portal) or run a self-hosted provider. Until then the node stays "
        "VALIDATION_PENDING. The first validation takes roughly 15 minutes; add --price to "
        "`node add` to override the default price."
    )
