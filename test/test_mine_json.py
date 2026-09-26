"""`lium mine --json`: one result object on stdout, one JSON step event per line on stderr, never a prompt.

An agent driving `lium mine` could not tell a port clash from a missing driver (both were a red line and exit 1).
Text mode prompts as it always has, reading the answers from stdin off a terminal.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.commands import mine
from lium.cli.commands import mine_register as reg
from provider._agent_mode import AGENT_SWITCHES, PLAIN_TEXT
from test_mine_register import HOTKEY, _Portal, _Resp, _http, _listing, _status, _stub_host, _token, _wait_no_sleep


def _invoke(args: list[str], env: dict | None = None, input: str | None = None):
    return CliRunner().invoke(mine.mine_command, args, env=env, input=input)


def _events(stderr: str) -> list[dict]:
    lines = [l for l in stderr.splitlines() if l.strip()]
    return [json.loads(l) for l in lines]   # every stderr line is JSON: a non-JSON line fails here


def _no_clone(monkeypatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(mine, "_clone_or_update_repo", lambda *a, **k: calls.append("clone"))
    return calls


def test_json_without_a_hotkey_is_input_required_and_touches_nothing(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    result = _invoke(["--json"])
    assert result.exit_code == 2, result.output
    body = json.loads(result.stdout)
    assert body["ok"] is False
    assert body["error"]["code"] == "input.input_required" and body["error"]["exit_code"] == 2
    assert body["data"]["field"] == "Miner hotkey SS58 address"
    assert calls == []


_TEXT = {"LIUM_OUTPUT": "", "LIUM_NONINTERACTIVE": ""}


def test_lium_noninteractive_without_a_hotkey_exits_two_and_touches_nothing(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    result = _invoke([], env={**_TEXT, "LIUM_NONINTERACTIVE": "1"}, input="5Fhotkey\n")
    assert result.exit_code == 2, result.output
    assert "asks nothing under LIUM_NONINTERACTIVE" in result.stderr and calls == []


def test_text_mode_off_a_terminal_reads_the_answers_from_stdin(monkeypatch, tmp_path: Path) -> None:
    target, executor_dir = _stub_host(monkeypatch, tmp_path)
    result = _invoke(["-k", HOTKEY, "--dir", str(target)], env=_TEXT, input="9090\n2201\n\n\n")
    assert result.exit_code == 0, result.output
    env = dict(l.split("=", 1) for l in (executor_dir / ".env").read_text().splitlines() if "=" in l)
    assert (env["INTERNAL_PORT"], env["SSH_PORT"]) == ("9090", "2201")


def test_text_mode_eof_at_a_prompt_exits_1(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    result = _invoke(["-k", HOTKEY], env=_TEXT, input="")   # stdin closes at the first port question
    assert result.exit_code == 1, result.output
    assert "Service port" in result.output and calls == []


def test_json_success_prints_one_result_and_step_events(monkeypatch, tmp_path: Path) -> None:
    target, executor_dir = _stub_host(monkeypatch, tmp_path)
    result = _invoke(["--json", "-k", HOTKEY, "--dir", str(target)])
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["ok"] is True
    assert body["data"]["endpoint"] == "203.0.113.7:8080"
    assert body["data"]["directory"] == str(executor_dir)
    assert body["data"]["add_command"].startswith("lium provider node add ")
    steps = [e for e in _events(result.stderr) if e["event"] == "step"]
    assert [(e["code"], e["status"]) for e in steps if e["status"] == "done"] == [
        ("host.repo", "done"), ("host.tools", "done"), ("host.prereqs", "done"),
        ("host.env", "done"), ("host.start", "done"), ("host.validate", "done"),
    ]
    assert all(e["total"] == 6 for e in steps)
    assert "\x1b[" not in result.stdout   # no spinner escapes on the result stream


def test_lium_output_json_is_the_same_as_the_flag(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    result = _invoke(["-k", HOTKEY, "--dir", str(target)], env={"LIUM_OUTPUT": "json"})
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["ok"] is True


def test_json_port_in_use_names_the_step_code_and_the_port(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(mine, "_check_ports_free", _real_check_ports_free)
    monkeypatch.setattr(mine, "_compose_project_running", lambda d: False)
    monkeypatch.setattr(mine, "_port_in_use", lambda port, host="0.0.0.0": port == 8080)
    monkeypatch.setattr(mine, "_listening_process", lambda port: "nginx")
    result = _invoke(["--json", "-k", HOTKEY, "--dir", str(target)])
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "host.port_in_use" and body["error"]["exit_code"] == 1
    assert body["data"] == {"step": "host.env", "port": 8080, "label": "service port", "owner": "nginx"}
    assert body["error"]["hint"]
    failed = [e for e in _events(result.stderr) if e.get("status") == "failed"]
    assert failed == [dict(failed[0], code="host.env", error_code="host.port_in_use")]


_real_check_ports_free = mine._check_ports_free


def test_json_missing_docker_is_host_docker_missing(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(mine, "_check_prereqs", _real_check_prereqs)
    monkeypatch.setattr(mine, "_exists", lambda cmd: cmd != "docker")
    result = _invoke(["--json", "-k", HOTKEY, "--dir", str(target)])
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["code"] == "host.docker_missing"


_real_check_prereqs = mine._check_prereqs


def test_json_an_uncoded_step_failure_gets_the_steps_code(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(mine, "_install_executor_tools",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("Command failed (1): apt [update]")))
    result = _invoke(["--json", "-k", HOTKEY, "--dir", str(target)])
    assert result.exit_code == 1
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "host.tools_failed"
    assert body["error"]["message"] == "Command failed (1): apt [update]"


def test_json_bad_register_token_is_input_exit_two(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    result = _invoke(["--json", "--register", _token(exp=int(time.time()) - 5)])
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "input.register_token_invalid"
    assert calls == []


def _register_portal(monkeypatch, *statuses: _Resp) -> None:
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _listing())
    portal.on("GET", "/executors/node-1", *statuses)
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    monkeypatch.setattr(reg, "wait_until_listed", _wait_no_sleep)


def test_json_register_listed_is_ok_with_the_node(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    _register_portal(monkeypatch, _status("VALIDATION_PENDING"), _status("AVAILABLE"))
    result = _invoke(["--json", "--register", _token(exp=int(time.time()) + 3600), "--dir", str(target), "--wait", "5"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)["data"]
    assert data["node_id"] == "node-1" and data["status"] == "AVAILABLE" and data["listed"] is True
    assert data["endpoint"] == "203.0.113.7:8080" and data["node_url"].endswith("/nodes/node-1")
    events = _events(result.stderr)
    assert {"host.register", "host.wait_listed"} <= {e.get("code") for e in events}
    assert [e["status"] for e in events if e["event"] == "node_status"][-1] == "AVAILABLE"


def test_json_register_named_fix_is_node_status_exit_one(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    _register_portal(monkeypatch, _status("OFFLINE", "Node not responding to ping."))
    result = _invoke(["--json", "--register", _token(exp=int(time.time()) + 3600), "--dir", str(target), "--wait", "1"])
    assert result.exit_code == 1, result.output
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "node.offline"
    assert "Node not responding to ping." in body["error"]["message"]


def test_json_register_not_listed_is_exit_eleven(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _Resp(200, {"data": []}))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))
    result = _invoke(["--json", "--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)])
    assert result.exit_code == 11, result.output
    body = json.loads(result.stdout)
    assert body["error"]["code"] == "node.not_listed_yet" and body["error"]["exit_code"] == 11
    assert body["data"]["endpoint"] == "203.0.113.7:8080"


def _not_listed_portal(monkeypatch) -> None:
    monkeypatch.setattr(reg, "FIND_NODE_RETRY_S", 0.0)
    portal = _Portal()
    portal.on("POST", "/executors", _Resp(200, {"success": True, "data": {"message": "queued"}}))
    portal.on("GET", "/executors", _Resp(200, {"data": []}))
    monkeypatch.setattr(reg, "build_http", lambda url, token: _http(portal))


def _mine_agent(switch, args: list[str]):
    flags, env = switch
    return _invoke([*flags, *args], env={**PLAIN_TEXT, **env})


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_a_bad_register_token_exits_2_under_every_agent_switch_and_1_in_plain_text(monkeypatch, switch) -> None:
    calls = _no_clone(monkeypatch)
    args = ["--register", _token(exp=int(time.time()) - 5)]
    assert _mine_agent(switch, args).exit_code == 2
    assert _invoke(args, env=PLAIN_TEXT).exit_code == 1
    assert calls == []


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_a_hotkey_the_token_does_not_name_exits_2_under_every_agent_switch_and_1_in_plain_text(monkeypatch, switch) -> None:
    calls = _no_clone(monkeypatch)
    args = ["--register", _token(exp=int(time.time()) + 3600, node_hotkey=HOTKEY), "-k", "5Other"]
    assert _mine_agent(switch, args).exit_code == 2
    assert _invoke(args, env=PLAIN_TEXT).exit_code == 1
    assert calls == []


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_registered_not_listed_exits_11_under_every_agent_switch(monkeypatch, tmp_path: Path, switch) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    _not_listed_portal(monkeypatch)
    result = _mine_agent(switch, ["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)])
    assert result.exit_code == 11, result.output


def test_registered_not_listed_exits_2_in_plain_text_as_before(monkeypatch, tmp_path: Path) -> None:
    target, _ = _stub_host(monkeypatch, tmp_path)
    _not_listed_portal(monkeypatch)
    result = _invoke(["--register", _token(exp=int(time.time()) + 3600), "--dir", str(target)], env=PLAIN_TEXT)
    assert result.exit_code == 2, result.output


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_mine_status_with_an_ss58_as_the_hotkey_name_exits_2_under_every_agent_switch(switch) -> None:
    flags, env = switch
    result = _invoke(["status", "node-1", "-k", HOTKEY, *flags], env={**PLAIN_TEXT, **env})
    assert result.exit_code == 2, result.output
    plain = _invoke(["status", "node-1", "-k", HOTKEY], env=PLAIN_TEXT)
    assert plain.exit_code == 1 and plain.stderr.startswith("[ARG_INVALID] ")


def test_mine_status_json_is_passed_on(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(mine, "_mine_status", lambda args, hotkey=None: seen.append(args) or 0)
    result = _invoke(["status", "node-1", "--json"])
    assert result.exit_code == 0 and seen == [["node-1", "--json"]]


def test_auto_help_says_what_it_does() -> None:
    result = _invoke(["--help"])
    flat = " ".join(result.output.split())
    assert "--auto" in flat and "input.input_required" in flat and "--json" in flat



def _add_block(output: str) -> str:
    tail = output.split("…or from this terminal:\n", 1)[1]
    return tail.split("\nValidators only reach", 1)[0]


def test_text_mode_prints_the_add_command_as_main_does(monkeypatch, tmp_path: Path) -> None:
    # main prints it as Rich markup, unescaped: a `[b]` in nvidia-smi's GPU name styles rather than shows, as on main
    for smi in ("NVIDIA L4\n", "NVIDIA [b]L4\n"):
        target, _ = _stub_host(monkeypatch, tmp_path, nvidia_smi=smi)
        result = _invoke(["-k", HOTKEY, "--auto", "--dir", str(target)], env=_TEXT)
        assert result.exit_code == 0, result.output
        assert _add_block(result.output) == (
            "lium provider node add --gpu-type 'NVIDIA L4' --gpu-count 1 --ip 203.0.113.7 \n--port 8080 --yes"
        ), result.output
