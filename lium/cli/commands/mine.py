"""Mine command for setting up a compute subnet executor/miner."""

import json
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

import click
from dotenv import dotenv_values
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


def _check_prereqs(compute_dir: Path | None = None):
    if not _exists("nvidia-smi"):
        raise Exception("NVIDIA GPU driver not found (nvidia-smi missing)")

    _run("nvidia-smi --query-gpu=name --format=csv,noheader")

    # Validators reject a node whose Docker has no sysbox-runc runtime; without this check the
    # provider learns it from the preflight or a validator cycle later. The installer also installs the
    # NVIDIA Container Toolkit, so it runs before the toolkit check.
    failure = _install_sysbox(compute_dir) if compute_dir and not _has_sysbox() else ""

    if not _exists("nvidia-container-cli"):
        raise Exception("NVIDIA Container Toolkit not found (required for Docker GPU access)")

    if not _exists("docker"):
        raise Exception("Docker not found")

    _run("docker info")

    if not _has_sysbox():
        raise Exception(
            "Sysbox runtime not found in Docker (required by validators).\n"
            f"Install it with: {_SYSBOX_SETUP_COMMAND}\n"
            "then run `lium mine` again." + (f"\n{failure}" if failure else "")
        )
    if failure:
        # the installer registers the runtime before its GPU checks, so a late failure still leaves sysbox-runc
        console.warning(f"The Sysbox installer reported a failure after installing the runtime:\n{failure}")


def _install_sysbox(compute_dir: Path) -> str:
    """Run the checkout's Sysbox installer once (DAH-4778: one manual step fewer). Same privilege rule as step 2's
    install script: root, or passwordless sudo. No terminal input, so its one confirmation is skipped.
    Returns the failure's tail, empty on success."""
    import os

    script = compute_dir / "neurons" / "executor" / "nvidia_docker_sysbox_setup.sh"
    if not script.exists():
        return ""
    sudo = "" if os.geteuid() == 0 else "sudo -n "
    try:
        _run(f"{sudo}bash {shlex.quote(str(script))} < /dev/null")
    except RuntimeError as exc:
        return str(exc)[-2000:]
    return ""


# Validators are to score 0 a node whose Docker cannot cap a container's disk (STORAGE_LIMIT_REQUIRED): one renter
# filling an uncapped disk fills /var/lib/docker and takes the host and every pod on it down (18 Oct 2026).
_STORAGE_GUIDE = "https://docs.lium.io/providers/nodes/docker-storage"


def _docker_storage_limited() -> bool:
    """True when Docker is overlay2 on xfs mounted with pquota, so `--storage-opt size=` caps a container."""
    from ..mine.docker_config import build_verification_result, inspect_docker_state
    from ..mine.gpu_splitting import run_command

    return build_verification_result(run_command, inspect_docker_state(run_command).docker_root_dir).passed


def _setup_docker_storage():
    """Offer to move Docker storage to an xfs pquota mount with `lium gpu-splitting setup`'s own plan and checks.

    One question is both the menu and the consent: every disk is listed and the person types the one to format, or
    presses Enter to skip. `--auto` and `--register` never consent. Whatever stops the setup, the manual command is
    printed and the install carries on (validators do not require it yet).
    """
    import os
    import sys

    from rich.prompt import Prompt

    from ..mine.gpu_splitting import _build_setup_plan, _execute_setup, _preflight, run_command
    from ..mine.storage import evaluate_candidates, get_device_by_path

    reason = "Docker cannot limit a container's disk on this host (needs xfs with pquota)."
    manual = (
        "Set it up with: sudo lium gpu-splitting setup --device <spare disk>\n"
        f"Guide: {_STORAGE_GUIDE}"
    )
    if not sys.stdin.isatty():
        console.warning(f"{reason}\n{manual}")
        return
    if os.geteuid() != 0:
        console.warning(f"{reason}\n" + manual.replace("Set it up with", "Run `sudo lium mine`, or set it up with"))
        return
    try:
        preflight = _preflight()
        candidates = evaluate_candidates(
            run_command, preflight.block_devices, preflight.root_mount, preflight.docker_mount
        )
    except RuntimeError as exc:
        console.warning(f"{reason}\n{manual}\nCould not list the disks: {exc}")
        return

    def row(candidate, note):
        device = get_device_by_path(preflight.block_devices, candidate.path)
        model = (device.model or "").strip() if device else ""
        serial = (device.serial or "").strip() if device else ""
        size = f"{device.size_bytes // 1024**3} GiB" if device else ""
        return f"  {candidate.path:<16} {model:<20} {serial:<16} {size:>10}   {note}"

    offered = [c for c in candidates if c.allowed]
    lines = [reason, f"Validators will soon score such nodes 0. Guide: {_STORAGE_GUIDE}", ""]
    lines += ["Disks that can be formatted for Docker storage:"]
    lines += [row(c, c.details) for c in offered] or ["  (none)"]
    rejected = [c for c in candidates if not c.allowed]
    if rejected:
        lines += ["", "Not offered:"] + [row(c, c.details) for c in rejected]
    if not offered:
        console.warning("\n".join(lines) + f"\n\n{manual}")
        return
    lines += [
        "",
        f"Setup will stop Docker, format the chosen disk, copy {preflight.docker_state.docker_root_dir} to it "
        "and start Docker again.",
    ]
    console.info("\n".join(lines))
    typed = Prompt.ask("Type the disk path to format it, or press Enter to skip", default="").strip()
    if not typed:
        console.warning(f"Skipped. {manual}")
        return
    if typed not in {c.path for c in offered}:
        console.warning(f"{typed} is not one of the disks listed above; nothing was changed.\n{manual}")
        return
    try:
        _execute_setup(_build_setup_plan(preflight, typed))
    except RuntimeError as exc:
        # a refusal (containers running, Docker down, no backup space, target checks) stops only the storage setup
        console.warning(f"Docker storage was not set up: {exc}\n{manual}")


# The printed command fetches the official installer, never the checkout's copy (what `lium mine` runs itself).
_SYSBOX_SETUP_COMMAND = (
    "curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-io/main/neurons/executor/"
    "nvidia_docker_sysbox_setup.sh | sudo bash"
)


def _has_sysbox() -> bool:
    # validators run --runtime=sysbox-runc, so the runtime key must match exactly, not as a substring
    runtimes, _ = _run("docker info --format '{{json .Runtimes}}'", check=False)
    try:
        parsed = json.loads(runtimes)
    except ValueError:
        return False
    return isinstance(parsed, dict) and "sysbox-runc" in parsed


# VerifyX refuses a node with under 100 GiB free, measured after the node images are pulled (~40 GiB: executor,
# runner, validator preflight). On 7 Oct 2026 a 100 GB disk passed every earlier check and failed only at step 6.
NODE_FREE_DISK_GIB = 100
NODE_IMAGES_GIB = 40
GIB = 1024**3


def _node_images_present(executor_dir: Path) -> bool:
    """Every image this run would pull is already local. The runner image carries the compose file with the executor
    digest the runner injects at build, so that file, not the unrendered docker-compose.app.yml, names the images."""
    try:
        images = set(_run("docker compose config --images", cwd=str(executor_dir))[0].split())
        runner = next(i for i in images if "executor-runner" in i)
        # inspect first: docker run pulls a missing image, which is the download this check exists to precede
        # (not --pull=never, which Docker before 20.10 rejects)
        _run(f"docker image inspect {shlex.quote(runner)}")
        baked = _run(f"docker run --rm --entrypoint cat {shlex.quote(runner)} /root/executor/docker-compose.yml")[0]
        images.update(re.findall(r"^\s*image:\s*(\S+)", baked, re.MULTILINE))
        images.add(PREFLIGHT_IMAGE)
        for image in images:
            _run(f"docker image inspect {shlex.quote(image)}")
    except (RuntimeError, StopIteration):
        return False
    return True


def _check_free_disk(executor_dir: Path):
    root = _run("docker info --format '{{.DockerRootDir}}'", check=False)[0].strip() or "/var/lib/docker"
    free = shutil.disk_usage(root if Path(root).exists() else "/").free
    needed_gib = NODE_FREE_DISK_GIB + (0 if _node_images_present(executor_dir) else NODE_IMAGES_GIB)
    if free < needed_gib * GIB:
        raise Exception(
            f"Not enough free disk: {free / GIB:.1f} GiB free on {root}, {needed_gib} GiB needed "
            f"(validation requires {NODE_FREE_DISK_GIB} GiB free after about {NODE_IMAGES_GIB} GiB of node images). "
            "Use a host with a disk of at least 160 GB, or free space and re-run."
        )


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

    # a re-run without --register rebuilds .env from the template; keep the claim the earlier --register wrote
    old_claim = _env_value(env_f, "MINER_ACCOUNT_CLAIM")

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
        elif k == "MINER_ACCOUNT_CLAIM" and old_claim:
            put(k, old_claim)
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
    if old_claim:
        required["MINER_ACCOUNT_CLAIM"] = old_claim
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
    ``EADDRINUSE`` means taken; ``EACCES`` on a privileged port from an
    unprivileged ``lium mine`` falls back to ``ss`` listing a listener there.
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
            if e.errno == errno.EACCES:
                # a port below 1024 this uid may not bind (compose, as root, can): ask ss for a listener instead,
                # so sshd on 22 or nginx on 443 still stops an unprivileged rerun before the executor is recreated
                return bool(_listening_process(port))
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
    health check time out three minutes later. When the executor's own
    ``executor`` service is already running (``executor_dir`` given), only the
    host ports it publishes now are exempt: those listeners are ours, while a
    changed port must be free before a recreate removes the working executor.
    """
    own: set[int] = set()
    if executor_dir is not None and _compose_project_running(executor_dir):
        # docker-compose.app.yml publishes ${EXTERNAL_PORT} and ${SSH_PORT} on the host
        own = {int(v) for k in ("EXTERNAL_PORT", "SSH_PORT") if (v := _executor_env_value(executor_dir, k) or "").isdigit()}
    for label, port in ports.items():
        if not port or port in own or not _port_in_use(port):
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


def _env_value(env_f: Path, key: str) -> str:
    try:
        lines = env_f.read_text().splitlines()
    except FileNotFoundError:
        return ""
    return next((line.split("=", 1)[1].strip() for line in lines if line.startswith(f"{key}=")), "")


def _executor_env_value(executor_dir: Path, key: str) -> Optional[str]:
    """``key`` in the environment the executor container was created with; None when there is no executor container."""
    cid = _run("docker compose -f docker-compose.app.yml ps -aq executor", check=False, cwd=str(executor_dir))[0].strip()
    if not cid:
        return None
    env = _run(f"docker inspect --format '{{{{range .Config.Env}}}}{{{{println .}}}}{{{{end}}}}' {cid}", check=False)[0]
    return next((line.split("=", 1)[1].strip() for line in env.splitlines() if line.startswith(f"{key}=")), "")


# .env keys the executor reads only at container start (env_file); a rerun that changes one must recreate it
_RESTART_KEYS = ("MINER_ACCOUNT_CLAIM", "INTERNAL_PORT", "EXTERNAL_PORT", "SSH_PORT", "SSH_PUBLIC_PORT", "RENTING_PORT_RANGE",
                 "RENTING_PORT_MAPPINGS")


def _restart_value(key: str, value: str):
    """``value`` as compared for a restart: NAT forwards as parsed JSON, so reformatting alone does not restart."""
    if key == "RENTING_PORT_MAPPINGS" and value:
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _executor_env_stale(executor_dir: Path) -> bool:
    """True when a running executor was created with a value of a ``_RESTART_KEYS`` key other than .env now holds."""
    env_f = executor_dir / ".env"
    for key in _RESTART_KEYS:
        running = _executor_env_value(executor_dir, key)
        if running is None:  # no executor container yet: a plain `up -d` creates it from .env
            return False
        if _restart_value(key, running) != _restart_value(key, _env_value(env_f, key)):
            return True
    return False


def _start_executor(executor_dir: Path, wait_secs: int = 180, recreate: bool = False):
    # Start using the default docker-compose.yml
    # recreate: an executor already running keeps its old .env, so a new MINER_ACCOUNT_CLAIM never reaches it;
    # a plain re-run leaves a running executor alone
    ps_cmd = "docker compose -f docker-compose.app.yml ps -q executor"
    # a restart under running rentals interrupts them; the validator names rental pods pod_* (lium-io const.py)
    names = _run("docker ps --filter status=created --filter status=running --format '{{.Names}}'", check=False)[0].split()
    rentals = [n for n in names if n.startswith("pod_")]
    if rentals and recreate:
        console.warning(
            f"{len(rentals)} rental(s) running on this node, so the executor was not restarted. "
            "The new settings are in .env and apply on its next restart; rerun this command once the rentals end."
        )
        recreate = False
    # after --force-recreate the old container can still report healthy; wait for one with a new ID
    old_id = _run(ps_cmd, check=False, cwd=str(executor_dir))[0].strip() if recreate else ""
    command = "docker compose up -d --force-recreate" if recreate else "docker compose up -d"
    if rentals:
        # even a plain `up -d` recreates a runner whose config changed, and the runner force-recreates the executor;
        # under rentals compose only starts what is missing
        command = "docker compose up -d --no-recreate"
    _run(command, capture=True, cwd=str(executor_dir))

    # Wait for the executor service to be fully healthy
    start = time.time()
    
    while time.time() - start < wait_secs:
        # Get the container name/ID for the executor service
        out, _ = _run(ps_cmd, cwd=str(executor_dir))
        if not out.strip() or out.strip() == old_id:
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
    internal: str, external: str, ssh: str, ssh_pub: str, rng: str, account_claim: str | None = None,
    mappings: str = "",
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
    if account_claim:
        set_or_append("MINER_ACCOUNT_CLAIM", account_claim)
    if mappings:   # the template rebuild dropped them
        set_or_append("RENTING_PORT_MAPPINGS", mappings)
    env_f.write_text("\n".join(content) + "\n")

def _check_renting_ports_apart(answers: dict, mappings: str = "") -> None:
    """A renting port that is also the service or SSH port would be handed to a pod while the executor holds it."""
    taken = {int(answers[k]) for k in ("internal_port", "external_port", "ssh_port", "ssh_public_port")
             if str(answers.get(k) or "").isdigit()}
    if mappings:  # validators drop a forward whose local or public port is the SSH port, leaving too few to list
        clash = sorted({p for pair in json.loads(mappings) for p in pair} & taken)
        if clash:
            raise click.UsageError(
                f"RENTING_PORT_MAPPINGS in the saved .env uses {', '.join(map(str, clash))}, which the node's service "
                "or SSH port would use; pick another service or SSH port."
            )
    elif answers["port_range"]:
        clash = sorted(_renting_ports(answers["port_range"]) & taken)
        if clash:
            raise click.UsageError(
                f"--renting-port-range includes {', '.join(map(str, clash))}, which the node's service or SSH port uses; "
                "pick a range without it."
            )


def _gather_inputs(
    hotkey: Optional[str],
    auto: bool,
    internal_port: int | None = None,
    external_port: int | None = None,
    ssh_port: int | None = None,
    port_range: str | None = None,
    ssh_public_port: str = "",
) -> dict:
    """Ask everything up-front; return a dict of resolved inputs."""
    answers = {}
    if auto:
        # Auto mode - use all defaults
        answers["hotkey"] = hotkey or ""
        internal = str(internal_port or 8080)
        answers.update(dict(
            internal_port=internal,
            external_port=str(external_port) if external_port else internal,
            ssh_port=str(ssh_port or 2200),
            ssh_public_port=ssh_public_port,
            port_range=port_range or "",
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
        service_port = ask_port("Service port (where the node API will be reachable)", internal_port or 8080)
        answers["internal_port"] = service_port
        answers["external_port"] = str(external_port) if external_port else service_port
        answers["ssh_port"] = ask_port("Node SSH port (used by validator to SSH into the container)", ssh_port or 2200)
        
        # Optional ports
        ssh_public = Prompt.ask("Public SSH port (optional, only if behind NAT and forwarding a different port)", default=ssh_public_port)
        answers["ssh_public_port"] = ssh_public if ssh_public and ssh_public.isdigit() else ""
        
        answers["port_range"] = Prompt.ask("Renting port range (optional, e.g. 2000-2005 or 2000,2001,2002). Leave empty if all ports open", default=port_range or "")

    return answers


PREFLIGHT_IMAGE = "daturaai/compute-subnet-validator:preflight"


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
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,  # only errors land here, small enough not to fill the pipe
        env=_subprocess_env(),
    )


def _finish_preflight_pull(pull) -> None:
    """Wait for the pull and raise on a failure.

    A failed pull must not fall through to a cached older image: it would refuse
    GPUs supported since it was built (RTX PRO 6000 Max-Q, 23 Oct 2026).
    """
    if pull.wait() != 0:
        err = (pull.stderr.read() if pull.stderr else "").strip()
        raise Exception(
            f"Could not pull the preflight image {PREFLIGHT_IMAGE}: {err[-500:] or 'docker pull failed'}. "
            f"Check that this host can reach Docker Hub (try `docker pull {PREFLIGHT_IMAGE}`), then rerun."
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
def _ipv4(ctx, param, value: str | None) -> str | None:
    if value is None:
        return None
    import ipaddress

    try:
        return str(ipaddress.IPv4Address(value))
    except ValueError:
        raise click.BadParameter(f"{value!r} is not an IPv4 address.")


def _renting_ports(value: str) -> set[int]:
    """Ports in a RENTING_PORT_RANGE value, read as the executor reads it: ``FIRST-LAST`` or a comma list."""
    m = re.fullmatch(r"(\d+)-(\d+)", value)
    parts = m.groups() if m else value.split(",")
    if not all(p.isdigit() and 1 <= int(p) <= 65535 for p in parts):
        raise click.BadParameter(
            f"{value!r}: use FIRST-LAST (e.g. 41000-41100) or a comma list (e.g. 41000,41001,41002), ports 1-65535.",
            param_hint="'--renting-port-range'",
        )
    ports = set(range(int(parts[0]), int(parts[1]) + 1)) if m else {int(p) for p in parts}
    if not ports:
        raise click.BadParameter(f"{value!r}: the first port is above the last.", param_hint="'--renting-port-range'")
    if len(ports) < 3:  # validators list no node with fewer than 3 renting ports
        raise click.BadParameter(
            f"{value!r}: give at least 3 ports; validators list no node with fewer.", param_hint="'--renting-port-range'"
        )
    return ports


def _renting_port_range(ctx, param, value: str | None) -> str | None:
    if value is None:
        return None
    value = value.replace(" ", "")
    _renting_ports(value)
    return value


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
@click.option(
    "--ip",
    callback=_ipv4,
    help="Public IPv4 the node is registered at, instead of the auto-detected one (a node behind CGNAT or a "
    "port-forwarding VPS: the VPS address).",
)
@click.option(
    "--internal-port",
    type=click.IntRange(1, 65535),
    default=None,
    help="Port the node's service listens on (default 8080).",
)
@click.option(
    "--external-port",
    type=click.IntRange(1, 65535),
    default=None,
    help="Port the node is registered at and validators connect to (default: --internal-port).",
)
@click.option(
    "--ssh-port",
    type=click.IntRange(1, 65535),
    default=None,
    help="Port validators SSH into the node on (SSH_PORT, default 2200). A second node behind the same "
    "public IP needs its own, e.g. 2201.",
)
@click.option(
    "--renting-port-range",
    callback=_renting_port_range,
    default=None,
    help="Ports rented pods are published on (RENTING_PORT_RANGE), e.g. 41000-41100 or 41000,41001,41002 "
    "(default: all ports). A second node behind the same public IP needs a range of its own.",
)
@click.pass_context
@handle_errors
def mine_command(ctx, hotkey, dir_, branch, auto, verbose, help_, register_token, portal_url, price, gpu_type,
                 wait_minutes, ip, internal_port, external_port, ssh_port, renting_port_range):
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
    IPv4 (or --ip) and the executor's port, at the model's base price from lium.io's public shared-config; then the node's
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
        # the claims were read without the portal's secret: the portal checks them before anything is installed
        try:
            reg.confirm_register_token(reg.build_http(portal_url, token.token))
        except reg.RegisterError as e:
            from rich.markup import escape

            console.error(f"❌ {escape(str(e))}")
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

    # a rerun keeps the port settings an earlier run wrote; a flag or a prompt answer overrides them
    saved_env = Path(dir_).absolute() / "neurons" / "executor" / ".env"
    # dotenv parsing: a saved value may carry quotes or an inline comment
    parsed = dotenv_values(saved_env) if saved_env.is_file() else {}
    saved = {k: parsed.get(k) or "" for k in ("INTERNAL_PORT", "EXTERNAL_PORT", "SSH_PORT", "SSH_PUBLIC_PORT", "RENTING_PORT_RANGE")}
    internal_port = internal_port or (int(saved["INTERNAL_PORT"]) if saved["INTERNAL_PORT"].isdigit() else None)
    if saved["EXTERNAL_PORT"].isdigit() and saved["EXTERNAL_PORT"] != saved["INTERNAL_PORT"]:  # equal: follows the service port
        external_port = external_port or int(saved["EXTERNAL_PORT"])
    ssh_port = ssh_port or (int(saved["SSH_PORT"]) if saved["SSH_PORT"].isdigit() else None)
    ssh_public = saved["SSH_PUBLIC_PORT"] if saved["SSH_PUBLIC_PORT"] != saved["SSH_PORT"] else ""  # equal: follows SSH_PORT
    renting_port_range = renting_port_range or saved["RENTING_PORT_RANGE"] or None
    answers = _gather_inputs(hotkey, auto, internal_port, external_port, ssh_port, renting_port_range, ssh_public)
    # the flag and the prompt answer get the same check: spaces dropped, format, size and overlap
    answers["port_range"] = answers["port_range"].replace(" ", "")
    host_ports = _host_ports_from_answers(answers)
    if len(set(host_ports.values())) < len(host_ports):   # compose cannot publish one host port twice
        raise click.UsageError(f"The service port and the SSH port are both {host_ports['SSH port']}; pick different ports.")
    public_ssh = answers["ssh_public_port"] or answers["ssh_port"]
    if str(answers["external_port"]) == str(public_ssh):   # NAT: both would need one public IP:port
        raise click.UsageError(f"The service port and the public SSH port are both {public_ssh}; pick different ports.")
    # NAT forwards win over the range in the validator: kept as written unless a different range replaces them
    mappings = ""
    if parsed.get("RENTING_PORT_MAPPINGS") and answers["port_range"] == saved["RENTING_PORT_RANGE"].replace(" ", ""):
        # the value dotenv parsed (spaces around =, quotes, several lines), written back as one line of JSON
        try:
            mappings = json.dumps(json.loads(parsed["RENTING_PORT_MAPPINGS"]), separators=(",", ":"))
        except ValueError:
            raise click.UsageError(f"RENTING_PORT_MAPPINGS in {saved_env} is not valid JSON; fix or remove it and rerun.")
    _check_renting_ports_apart(answers, mappings)
    target_dir = Path(dir_).absolute()

    TOTAL_STEPS = 8 if token else 6

    try:
        with timed_step_status(1, TOTAL_STEPS, "Ensuring repository"):
            _clone_or_update_repo(target_dir, branch)

        with timed_step_status(2, TOTAL_STEPS, "Installing node tools"):
            _install_executor_tools(target_dir)

        with timed_step_status(3, TOTAL_STEPS, "Checking prerequisites"):
            _check_prereqs(target_dir)
            storage_ok = _docker_storage_limited()
        if not storage_ok:
            _setup_docker_storage()   # outside the spinner: it prints the plan and may ask to confirm
        _check_free_disk(target_dir / "neurons" / "executor")

        # Docker is confirmed; fetch the preflight image while steps 4–5 run.
        preflight_pull = _start_preflight_pull()

        with timed_step_status(4, TOTAL_STEPS, "Configuring environment"):
            executor_dir = target_dir / "neurons" / "executor"
            if not executor_dir.exists():
                raise Exception(f"Node directory not found at {executor_dir}")

            # A taken host port makes `docker compose up` loop on
            # "address already in use" and the health check below time out
            # with no explanation. Catch it here, before .env changes or the 3-minute wait.
            _check_ports_free(host_ports, executor_dir)

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
                account_claim=token.account_claim if token else None,
                mappings=mappings,
            )

        with timed_step_status(5, TOTAL_STEPS, "Starting node"):
            # recreate only an executor whose claim or ports differ from .env: a repeat run leaves it alone, and a
            # rerun after a restart skipped under rentals still delivers what .env already holds
            _start_executor(executor_dir, recreate=_executor_env_stale(executor_dir))

        console.dim(
            "Validation runs the validator's preflight image: GPU check, matrix "
            "work-proof and VerifyX (RAM, disk throughput, network). Typically 2–4 minutes."
        )
        step6 = _StepMessage("Validating node")
        with timed_step_status(6, TOTAL_STEPS, step6):
            if preflight_pull.poll() is None:
                step6.detail = "pulling preflight image"
            _finish_preflight_pull(preflight_pull)

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
            ip_override=ip,
        ))

    # Get executor details for summary
    gpu_info = _get_gpu_info()
    public_ip = ip or _get_public_ip()
    
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
    # soft_wrap: a hard wrap split the IP across lines and the clicked link opened Add Node with a wrong address
    console.print(f"[yellow]{add_url}[/yellow]\n", soft_wrap=True)
    console.print("[bold cyan]…or from this terminal:[/bold cyan]")
    console.print(f"[yellow]{_provider_add_command(gpu_info, public_ip, external_port)}[/yellow]", soft_wrap=True)
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
    ip_override: str | None = None,
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
            ip = ip_override or reg.public_ipv4_or_fail(_get_public_ip())
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
        "If your account is not opted in to the Lium Central Provider Server yet (Profile Settings "
        "in the portal; on by default for new accounts), run `lium provider config opt-in --yes` "
        "or a self-hosted provider, or the node stays VALIDATION_PENDING. The first validation "
        "takes roughly 15 minutes. The node is listed at the model's default price; add --price "
        "to `node add` to set your own within the range the portal shows for the model."
    )
