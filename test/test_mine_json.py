"""`lium mine --json`: one result object on stdout, one JSON step event per line on stderr, never a prompt.

An agent driving `lium mine` could not tell a port clash from a missing driver (both were a red line and exit 1),
and without a terminal the hotkey prompt ended in an EOF traceback.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner

from lium.cli.commands import mine
from lium.cli.commands import mine_register as reg
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


def test_no_terminal_without_a_hotkey_exits_two_instead_of_an_eof_traceback(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    result = _invoke([])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "no terminal to ask" in result.stderr and calls == []


def test_eof_at_a_prompt_is_input_required(monkeypatch) -> None:
    calls = _no_clone(monkeypatch)
    monkeypatch.setattr(mine, "is_interactive", lambda: True)
    result = _invoke(["-k", HOTKEY], input="")   # stdin closes at the first port question
    assert result.exit_code == 2, result.output
    assert not isinstance(result.exception, EOFError)
    assert "Service port" in result.stderr and calls == []


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


def test_mine_status_json_is_passed_on(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(mine, "_mine_status", lambda args, hotkey=None: seen.append(args) or 0)
    result = _invoke(["status", "node-1", "--json"])
    assert result.exit_code == 0 and seen == [["node-1", "--json"]]


def test_auto_help_says_what_it_does() -> None:
    result = _invoke(["--help"])
    flat = " ".join(result.output.split())
    assert "--auto" in flat and "input.input_required" in flat and "--json" in flat
