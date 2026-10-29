"""`lium mine` on a fresh Ubuntu 24.04 box, as run by the persona test on 7 Sep 2026 (personas/provider.sh):

* step 2 (`install_executor_on_ubuntu.sh`) died with `apt-get: …/_internal/libstdc++.so.6: version GLIBCXX_3.4.32 not
  found` — the PyInstaller bundle's LD_LIBRARY_PATH leaked into the child;
* the failure panel showed the FIRST 4000 chars of the tool's output (pull progress), not the error at the end (B-83);
* after `❌ Command failed (1)` the command exited 0;
* a blank "Public SSH port" left the template's `SSH_PUBLIC_PORT=2200` beside `SSH_PORT=30311` (B-82) — the executor
  advertises `SSH_PUBLIC_PORT or SSH_PORT`, so validators were sent to a port nothing listens on.
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setattr(mine, "_gather_inputs", lambda hotkey, auto, *ports: {
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
    monkeypatch.setattr(mine, "_gather_inputs", lambda hotkey, auto, *ports: {
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
def test_prereqs_fail_without_a_sysbox_runc_runtime_and_print_the_official_installer(monkeypatch, runtimes):
    _prereq_host(monkeypatch, runtimes)
    with pytest.raises(Exception) as err:
        mine._check_prereqs()
    assert mine._SYSBOX_SETUP_COMMAND in str(err.value)


def _sysbox_install_host(monkeypatch, tmp_path, *, install_ok: bool, registers_before_failing: bool = False,
                         toolkit_present: bool = True):
    script = tmp_path / "neurons" / "executor" / "nvidia_docker_sysbox_setup.sh"
    script.parent.mkdir(parents=True)
    script.write_text("")
    ran = []
    installed = {"done": False}

    def run(cmd, **kw):
        ran.append(cmd)
        if "sysbox_setup" in cmd:
            installed["done"] = install_ok or registers_before_failing
            if not install_ok:
                raise RuntimeError("Command failed (1): kernel too old")
        if "Runtimes" in cmd:
            return ('{"sysbox-runc":{}}' if installed["done"] else '{"runc":{}}'), ""
        return "", ""

    # without a toolkit up front, only the installer puts it there
    monkeypatch.setattr(mine, "_exists", lambda cmd: cmd != "nvidia-container-cli" or toolkit_present or installed["done"])
    monkeypatch.setattr(mine, "_run", run)
    return script, ran


def test_prereqs_install_sysbox_from_the_checkout_without_asking(monkeypatch, tmp_path):
    script, ran = _sysbox_install_host(monkeypatch, tmp_path, install_ok=True, toolkit_present=False)

    mine._check_prereqs(tmp_path)

    install = [cmd for cmd in ran if "sysbox_setup" in cmd]
    assert len(install) == 1 and str(script) in install[0] and install[0].endswith("< /dev/null")


def test_a_failed_sysbox_install_stops_with_the_manual_command_and_the_cause(monkeypatch, tmp_path):
    _sysbox_install_host(monkeypatch, tmp_path, install_ok=False)

    with pytest.raises(Exception) as err:
        mine._check_prereqs(tmp_path)

    assert mine._SYSBOX_SETUP_COMMAND in str(err.value) and "kernel too old" in str(err.value)


def test_a_late_sysbox_installer_failure_with_the_runtime_present_is_printed_as_a_warning(monkeypatch, tmp_path):
    _sysbox_install_host(monkeypatch, tmp_path, install_ok=False, registers_before_failing=True)
    warnings = []
    monkeypatch.setattr(mine.console, "warning", warnings.append)

    mine._check_prereqs(tmp_path)

    assert len(warnings) == 1 and "kernel too old" in warnings[0]


def test_prereqs_pass_with_sysbox(monkeypatch):
    _prereq_host(monkeypatch, '{"runc":{"path":"runc"},"sysbox-runc":{"path":"/usr/bin/sysbox-runc"}}')
    mine._check_prereqs()


def _storage_host(monkeypatch, *, root: bool = True, tty: bool = True, typed: str = "",
                  plan_error: Exception | None = None, execute_error: Exception | None = None):
    import lium.cli.mine.gpu_splitting as gs
    import lium.cli.mine.storage as st
    from rich.prompt import Prompt

    from lium.cli.mine.models import BlockDevice, CandidateEvaluation

    def disk(path, model):
        return BlockDevice(path=path, name=path[5:], kernel_name=path[5:], type="disk", size_bytes=3576 * 1024**3,
                           fstype=None, mountpoints=[], parent_kernel_name=None, model=model, serial="S6UK")

    calls = []
    preflight = SimpleNamespace(
        block_devices=[disk("/dev/nvme1n1", "Samsung PM9A3"), disk("/dev/sdb", "WDC WD40EFRX"),
                       disk("/dev/nvme0n1", "Micron 7450")],
        root_mount=None, docker_mount=None, docker_state=SimpleNamespace(docker_root_dir="/var/lib/docker"),
    )
    candidates = [
        CandidateEvaluation("/dev/nvme1n1", "disk", "blank_whole_disk", True, "blank whole disk"),
        CandidateEvaluation("/dev/sdb", "disk", "blank_whole_disk", True, "blank whole disk"),
        CandidateEvaluation("/dev/nvme0n1", "disk", "protected_root_or_docker", False, "protected root"),
    ]
    monkeypatch.setattr("os.geteuid", lambda: 0 if root else 1000)
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: tty))
    monkeypatch.setattr(Prompt, "ask", lambda *a, **k: calls.append("asked") or typed)

    def plan(pf, device):
        calls.append(("plan", device))
        if plan_error:
            raise plan_error
        return "plan"

    def execute(p):
        calls.append(("execute", p))
        if execute_error:
            raise execute_error

    monkeypatch.setattr(gs, "_preflight", lambda: preflight)
    monkeypatch.setattr(st, "evaluate_candidates", lambda *a: candidates)
    monkeypatch.setattr(gs, "_build_setup_plan", plan)
    monkeypatch.setattr(gs, "_execute_setup", execute)
    return calls


def test_storage_setup_never_formats_without_a_terminal_whatever_the_flags(monkeypatch, capsys):
    # --auto and --register run without a person at the keyboard: no prompt, no plan, no format
    calls = _storage_host(monkeypatch, tty=False)
    mine._setup_docker_storage()
    assert calls == []
    assert "lium gpu-splitting setup --device" in capsys.readouterr().out


def test_storage_setup_without_root_prints_the_sudo_command(monkeypatch, capsys):
    calls = _storage_host(monkeypatch, root=False)
    mine._setup_docker_storage()
    assert calls == []
    assert "sudo lium mine" in capsys.readouterr().out


def test_storage_setup_lists_every_disk_and_enter_skips(monkeypatch, capsys):
    calls = _storage_host(monkeypatch, typed="")
    mine._setup_docker_storage()
    out = capsys.readouterr().out
    assert calls == ["asked"]
    assert "Samsung PM9A3" in out and "WDC WD40EFRX" in out and "3576 GiB" in out
    assert "Not offered" in out and "/dev/nvme0n1" in out and "Micron 7450" in out
    assert "lium gpu-splitting setup --device" in out


def test_storage_setup_formats_only_the_typed_listed_disk_with_no_second_prompt(monkeypatch):
    calls = _storage_host(monkeypatch, typed="/dev/sdb")
    mine._setup_docker_storage()
    assert calls == ["asked", ("plan", "/dev/sdb"), ("execute", "plan")]


@pytest.mark.parametrize("typed", ["/dev/nvme0n1", "yes", "/dev/sdz"])
def test_storage_setup_anything_but_a_listed_disk_changes_nothing(monkeypatch, typed):
    calls = _storage_host(monkeypatch, typed=typed)
    mine._setup_docker_storage()
    assert calls == ["asked"]


def test_storage_setup_refusal_before_docker_stops_does_not_abort_the_install(monkeypatch, capsys):
    calls = _storage_host(monkeypatch, typed="/dev/sdb", execute_error=RuntimeError("Containers are running on this host"))
    mine._setup_docker_storage()
    out = capsys.readouterr().out
    assert calls == ["asked", ("plan", "/dev/sdb"), ("execute", "plan")]
    assert "Containers are running" in out and "lium gpu-splitting setup --device" in out


def test_storage_setup_target_check_refusal_names_the_manual_command(monkeypatch, capsys):
    calls = _storage_host(monkeypatch, typed="/dev/sdb", plan_error=RuntimeError("Refusing /dev/sdb: it is a removable disk"))
    mine._setup_docker_storage()
    out = capsys.readouterr().out
    assert calls == ["asked", ("plan", "/dev/sdb")]
    assert "removable" in out and "lium gpu-splitting setup --device" in out


_HK = "5F" + "x" * 46


def _capture_answers(monkeypatch) -> dict:
    """Run `lium mine` up to the first step; return the answers it would write into .env."""
    seen = {}
    real = mine._gather_inputs

    def gather(*a):
        seen.update(real(*a))
        return seen

    def stop(*a, **k):
        raise RuntimeError("stop after gathering inputs")

    monkeypatch.setattr(mine, "_gather_inputs", gather)
    monkeypatch.setattr(mine, "_clone_or_update_repo", stop)
    return seen


def test_ssh_port_and_renting_port_range_reach_the_env(monkeypatch, tmp_path: Path):
    """A second node behind one public IP: its own SSH port and renting range go into .env, no hand edit."""
    answers = _capture_answers(monkeypatch)
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK, "--external-port", "8081",
                                                   "--ssh-port", "2201", "--renting-port-range", "41000-41100"])
    assert "stop after gathering inputs" in result.output
    env = tmp_path / ".env"
    env.write_text("SSH_PORT=2200\nSSH_PUBLIC_PORT=2200\nRENTING_PORT_RANGE=\n")
    mine._apply_env_overrides(tmp_path, internal=answers["internal_port"], external=answers["external_port"],
                              ssh=answers["ssh_port"], ssh_pub=answers["ssh_public_port"], rng=answers["port_range"])
    lines = env.read_text().splitlines()
    assert {"EXTERNAL_PORT=8081", "SSH_PORT=2201", "SSH_PUBLIC_PORT=2201", "RENTING_PORT_RANGE=41000-41100"} <= set(lines)


def test_defaults_without_the_new_flags(monkeypatch):
    answers = _capture_answers(monkeypatch)
    CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK])
    assert answers["ssh_port"] == "2200" and answers["port_range"] == ""


@pytest.mark.parametrize("value", ["41000-", "41100-41000", "40000-70000", "a-b", "41000,,41001", "0-10", "41000", "41000,41001", "41000-41001"])
def test_bad_renting_port_range_is_a_usage_error(value):
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK, "--renting-port-range", value])
    assert result.exit_code == 2, result.output
    assert "--renting-port-range" in result.output


def test_comma_list_renting_ports_are_accepted(monkeypatch):
    answers = _capture_answers(monkeypatch)
    CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK, "--renting-port-range", "41000, 41001, 41002"])
    assert answers["port_range"] == "41000,41001,41002"


@pytest.mark.parametrize("flags", [["--ssh-port", "41050"], ["--internal-port", "41000"], ["--external-port", "41100"]])
def test_renting_range_overlapping_another_port_is_refused(monkeypatch, flags):
    _capture_answers(monkeypatch)
    result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK, "--renting-port-range", "41000-41100", *flags])
    assert result.exit_code == 2, result.output
    assert "pick a range without it" in result.output


@pytest.mark.parametrize("flags, prompted, ok", [
    (["--ssh-port", "2201"], "2200-2202", False),               # prompt answer overlapping the SSH flag
    (["--renting-port-range", "41000-41100"], "41000, 41001, 41002", True),  # spaced prompt answer over a flag default
    ([], "41000-41100", True),
    ([], "41000,41001", False),
    ([], "41000-x", False),
])
def test_prompted_renting_range_gets_the_flag_checks(monkeypatch, flags, prompted, ok):
    """Interactive answers are normalized and refused exactly like the flag."""
    answers = _capture_answers(monkeypatch)
    monkeypatch.setattr(mine.Prompt, "ask", lambda text, default="", **k: prompted if "Renting" in text else default)
    result = CliRunner().invoke(mine.mine_command, ["-k", _HK, *flags])
    assert (result.exit_code != 2) == ok, result.output
    if ok:
        assert answers["port_range"] == prompted.replace(" ", "")
    else:
        assert "--renting-port-range" in result.output


@pytest.mark.parametrize("running, rentals, up, rerun_without_flags, env_format", [
    (None, False, "docker compose up -d", False, "plain"),                                    # first installation
    ({"SSH_PORT": "2201"}, False, "docker compose up -d", False, "plain"),                    # unchanged rerun
    ({"SSH_PORT": "2200"}, False, "docker compose up -d --force-recreate", False, "plain"),   # changed SSH port
    ({"RENTING_PORT_RANGE": ""}, False, "docker compose up -d --force-recreate", False, "plain"),  # changed renting range
    ({"SSH_PORT": "2200"}, True, "docker compose up -d --no-recreate", False, "plain"),       # changed, but rentals running
    ({"SSH_PORT": "2201"}, False, "docker compose up -d", True, "plain"),                     # rerun without port flags keeps .env
    ({"SSH_PORT": "2201"}, False, "docker compose up -d", True, "commented"),  # saved values with inline comments
    ({"SSH_PORT": "2201"}, False, "docker compose up -d", True, "quoted"),     # quoted saved port values
    ({"SSH_PORT": "2201"}, False, "docker compose up -d", True, "quoted_range"),  # quoted saved renting range
])
def test_rerun_with_changed_ports_recreates_the_executor(
    monkeypatch, tmp_path: Path, running, rentals, up, rerun_without_flags, env_format
):
    env = {"MINER_ACCOUNT_CLAIM": "", "INTERNAL_PORT": "8080", "EXTERNAL_PORT": "8081", "SSH_PORT": "2201",
           "SSH_PUBLIC_PORT": "2201", "RENTING_PORT_RANGE": "41000-41100"}
    (tmp_path / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    container = None if running is None else "\n".join(f"{k}={v}" for k, v in {**env, **running}.items())
    calls, ids = [], iter(["old", "new"])

    def fake_run(cmd, check=True, capture=True, cwd=None):
        calls.append(cmd)
        if cmd.startswith("docker ps --filter"):
            return ("pod_abc\n" if rentals else ""), ""
        if "ps -aq executor" in cmd:
            return ("cid" if container else ""), ""
        if ".Config.Env" in cmd:
            return container, ""
        if "ps -q executor" in cmd:
            return next(ids, "new"), ""
        if cmd.startswith("docker inspect"):
            return "healthy", ""
        return "", ""

    monkeypatch.setattr(mine, "_run", fake_run)
    monkeypatch.setattr(mine.time, "sleep", lambda s: None)
    monkeypatch.setattr(mine.console, "warning", lambda *a: None)
    if rerun_without_flags:
        target = tmp_path / "repo"
        node = target / "neurons" / "executor"
        node.mkdir(parents=True)
        numeric = {"INTERNAL_PORT", "EXTERNAL_PORT", "SSH_PORT", "SSH_PUBLIC_PORT"}
        lines = []
        for key, value in env.items():
            if env_format == "commented" and key in numeric:
                value += " # configured node port"
            elif env_format == "quoted" and key in numeric or env_format == "quoted_range" and key == "RENTING_PORT_RANGE":
                value = f'"{value}"'
            lines.append(f"{key}={value}\n")
        (node / ".env").write_text("".join(lines))
        (node / ".env.template").write_text(
            "MINER_HOTKEY_SS58_ADDRESS=\nINTERNAL_PORT=8080\nEXTERNAL_PORT=8080\nSSH_PORT=2200\nSSH_PUBLIC_PORT=2200\n"
        )
        for name in ("_clone_or_update_repo", "_install_executor_tools", "_check_prereqs", "_check_free_disk",
                     "_check_ports_free"):
            monkeypatch.setattr(mine, name, lambda *a, **k: None)
        monkeypatch.setattr(mine, "_docker_storage_limited", lambda: True)
        monkeypatch.setattr(mine, "_start_preflight_pull", lambda: SimpleNamespace(poll=lambda: 0))

        def stop_after_start(*a):
            raise SystemExit(99)

        monkeypatch.setattr(mine, "_finish_preflight_pull", stop_after_start)
        result = CliRunner().invoke(mine.mine_command, ["--auto", "-k", _HK, "--dir", str(target)])
        assert result.exit_code == 99, result.output
        assert {key: mine._env_value(node / ".env", key) for key in env} == env
    else:
        mine._start_executor(tmp_path, wait_secs=5, recreate=mine._executor_env_stale(tmp_path))
    assert [c for c in calls if c.startswith("docker compose up")] == [up]
