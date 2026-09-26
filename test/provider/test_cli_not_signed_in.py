"""Every provider command that needs a sign-in, run with none: ``auth.not_signed_in`` (exit 6) in agent mode,
the old ``ARG_INVALID`` line (exit 1) in text mode. The commands come from walking the ``provider`` group, so a
new command is covered without a new row here."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from ._portal_stub import PortalStub

# These need the hotkey itself, not just a sign-in: they keep `input.arg_invalid` (exit 2 under --json).
HOTKEY_ONLY = {("portal", "login"), ("portal", "logout"), ("status",)}

_ARGUMENTS = {"tier": "secure", "count": "1"}
_OPTIONS = {"--scope": "read"}
SIGNED_OUT = {"LIUM_PROVIDER_TOKEN": "", "LIUM_PROVIDER_EMAIL": "", "LIUM_OUTPUT": "", "LIUM_NONINTERACTIVE": "", "LIUM_PROVIDER_ACK": ""}


def _leaves(group: click.Group, path: tuple[str, ...] = ()):
    for name, cmd in sorted(group.commands.items()):
        if isinstance(cmd, click.Group):
            yield from _leaves(cmd, (*path, name))
        else:
            yield (*path, name), cmd


def _invocation(path: tuple[str, ...], cmd: click.Command) -> list[str]:
    args = list(path)
    for param in cmd.params:
        if isinstance(param, click.Argument) and param.required:
            args.append(_ARGUMENTS.get(param.name, "7c1f0e2a-0000-4000-8000-000000000001"))
        elif isinstance(param, click.Option) and param.required:
            args += [param.opts[0], _OPTIONS.get(param.opts[0], "1")]
    return args


SIGNED_IN_COMMANDS = [
    pytest.param(_invocation(path, cmd), id=" ".join(path))
    for path, cmd in _leaves(provider_command)
    if path not in HOTKEY_ONLY
]


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    stub = PortalStub()
    yield stub
    stub.close()


def _run(portal: PortalStub, *args: str, env: dict | None = None):
    return CliRunner().invoke(provider_command, ["--portal-url", portal.url, *args], env={**SIGNED_OUT, **(env or {})})


def test_the_walk_finds_the_commands_an_agent_uses() -> None:
    ids = {p.id for p in SIGNED_IN_COMMANDS}
    assert {"node listing", "node pause", "node status", "portal whoami", "earnings", "token list"} <= ids
    assert len(ids) >= 40


@pytest.mark.parametrize("args", SIGNED_IN_COMMANDS)
def test_agent_mode_without_a_sign_in_is_auth_not_signed_in_exit_6(portal, args) -> None:
    result = _run(portal, "--json", *args)
    assert result.exit_code == 6, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["legacy_code"], error["exit_code"]) == ("auth.not_signed_in", "ARG_INVALID", 6)
    assert error["message"] == "not signed in to the provider portal"
    for way in ("LIUM_PROVIDER_TOKEN", "portal login --email", "--hotkey"):
        assert way in error["hint"]
    assert portal.requests == []


@pytest.mark.parametrize("args", [a for a in SIGNED_IN_COMMANDS if a.id != "portal whoami"])
def test_text_mode_without_a_sign_in_keeps_the_old_line_and_exit_1(portal, args) -> None:
    result = _run(portal, *args)
    assert result.exit_code == 1, result.output
    first, hint = result.stderr.splitlines()[:2]
    assert first.startswith("[ARG_INVALID] ") and first.endswith(" require --hotkey (or LIUM_PROVIDER_HOTKEY)")
    assert hint == "  hint: Check the argument value and consult --help."
    assert portal.requests == []


def test_lium_output_json_is_agent_mode_too(portal) -> None:
    result = _run(portal, "node", "listing", env={"LIUM_OUTPUT": "json"})
    assert result.exit_code == 6 and json.loads(result.stdout)["error"]["code"] == "auth.not_signed_in"


def test_noninteractive_text_prints_the_new_message_under_the_old_label(portal) -> None:
    result = _run(portal, "node", "listing", env={"LIUM_NONINTERACTIVE": "1"})
    assert result.exit_code == 1, "text output keeps the text map's exit, as every other error does"
    assert result.stderr.startswith("[ARG_INVALID] not signed in to the provider portal\n  hint: Set LIUM_PROVIDER_TOKEN")


@pytest.mark.parametrize("args", [("portal", "login"), ("portal", "logout"), ("status",)])
def test_the_hotkey_only_commands_keep_input_arg_invalid(portal, args) -> None:
    result = _run(portal, "--json", *args)
    assert result.exit_code == 2 and json.loads(result.stdout)["error"]["code"] == "input.arg_invalid"
