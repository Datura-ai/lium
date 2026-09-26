"""Every provider command that needs a sign-in, run with none: ``auth.not_signed_in`` (exit 6) in agent mode
(``--json``, ``LIUM_OUTPUT=json`` or ``LIUM_NONINTERACTIVE=1``), the old ``ARG_INVALID`` line (exit 1) in plain
text mode. The commands come from walking the ``provider`` group, so a new command is covered without a new row
here."""

from __future__ import annotations

import json

import click
import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.client import email_session_key
from lium.provider.token_store import TokenStore
from ._agent_mode import AGENT_SWITCHES, read_error
from ._portal_stub import PortalStub

# These need the hotkey itself, not just a sign-in: they keep `input.arg_invalid` (exit 2 in agent mode).
WALLET_SIGNED = {("config", "set-email"), ("config", "set-password")}
HOTKEY_ONLY = {("portal", "login"), ("portal", "logout"), ("status",), *WALLET_SIGNED}

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


def _agent_run(portal: PortalStub, switch, *args: str, env: dict | None = None):
    flags, switch_env = switch
    result = _run(portal, *flags, *args, env={**switch_env, **(env or {})})
    return (result, *read_error(result, switch))


def test_the_walk_finds_the_commands_an_agent_uses() -> None:
    ids = {p.id for p in SIGNED_IN_COMMANDS}
    assert {"node listing", "node pause", "node status", "portal whoami", "earnings", "token list"} <= ids
    assert len(ids) >= 40
    assert len(HOTKEY_ONLY_COMMANDS) == len(HOTKEY_ONLY)


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
@pytest.mark.parametrize("args", SIGNED_IN_COMMANDS)
def test_agent_mode_without_a_sign_in_is_auth_not_signed_in_exit_6(portal, args, switch) -> None:
    result, code, message, hint = _agent_run(portal, switch, *args)
    assert (result.exit_code, code) == (6, "auth.not_signed_in"), result.output
    assert message == "not signed in to the provider portal"
    for way in ("LIUM_PROVIDER_TOKEN", "portal login --email", "--hotkey"):
        assert way in hint
    assert portal.requests == []


def test_the_envelope_keeps_the_old_code(portal) -> None:
    error = json.loads(_run(portal, "--json", "node", "listing").stdout)["error"]
    assert (error["code"], error["legacy_code"], error["exit_code"]) == ("auth.not_signed_in", "ARG_INVALID", 6)


@pytest.mark.parametrize("args", [a for a in SIGNED_IN_COMMANDS if a.id != "portal whoami"])
def test_text_mode_without_a_sign_in_keeps_the_old_line_and_exit_1(portal, args) -> None:
    result = _run(portal, *args)
    assert result.exit_code == 1, result.output
    first, hint = result.stderr.splitlines()[:2]
    assert first.startswith("[ARG_INVALID] ") and first.endswith(" require --hotkey (or LIUM_PROVIDER_HOTKEY)")
    assert hint == "  hint: Check the argument value and consult --help."
    assert portal.requests == []


def test_noninteractive_text_prints_the_namespaced_code_and_exits_6(portal) -> None:
    result = _run(portal, "node", "listing", env={"LIUM_NONINTERACTIVE": "1"})
    assert result.exit_code == 6
    assert result.stdout == ""
    assert result.stderr.startswith("[auth.not_signed_in] not signed in to the provider portal\n  hint: Set LIUM_PROVIDER_TOKEN")


def test_a_configured_address_with_no_session_is_named_as_such(portal) -> None:
    result, code, message, _ = _agent_run(portal, AGENT_SWITCHES[0].values[0], "node", "listing",
                                          env={"LIUM_PROVIDER_EMAIL": "ops@example.com"})
    assert (result.exit_code, code) == (6, "auth.not_signed_in")
    assert message == "not signed in to the provider portal (no live e-mail session for ops@example.com)"


HOTKEY_ONLY_COMMANDS = [
    pytest.param(_invocation(path, cmd), id=" ".join(path)) for path, cmd in _leaves(provider_command) if path in HOTKEY_ONLY
]


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
@pytest.mark.parametrize("args", HOTKEY_ONLY_COMMANDS)
def test_the_hotkey_only_commands_keep_input_arg_invalid(portal, args, switch) -> None:
    result, code, _, _ = _agent_run(portal, switch, *args)
    assert (result.exit_code, code) == (2, "input.arg_invalid"), result.output
    assert portal.requests == []


SIGNED_IN_ELSEWHERE = [
    pytest.param({"LIUM_PROVIDER_TOKEN": "lium_pt_x"}, id="token"),
    pytest.param({"LIUM_PROVIDER_EMAIL": "ops@example.com"}, id="email-session"),
]


def _save_session(email: str) -> None:
    TokenStore().save(email_session_key(email), "session-stub", provider_id="m-1")


@pytest.mark.parametrize("signed_in", SIGNED_IN_ELSEWHERE)
@pytest.mark.parametrize("switch", AGENT_SWITCHES)
@pytest.mark.parametrize("path", sorted(WALLET_SIGNED))
def test_wallet_signed_commands_refuse_a_token_or_e_mail_session_in_agent_mode(portal, path, switch, signed_in) -> None:
    _save_session("ops@example.com")
    args = [*path, "x@example.com"] if path[-1] == "set-email" else [*path, "--password", "pw-123456"]
    result, code, message, hint = _agent_run(portal, switch, "-y", *args, env=signed_in)
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert (result.exit_code, code) == (2, "input.arg_invalid"), result.output
    assert message == (f"{' '.join(path)} requires --hotkey (or LIUM_PROVIDER_HOTKEY): it signs with the hotkey's "
                       "wallet, and a provider API token or e-mail session cannot")
    assert "--hotkey" in hint and "LIUM_PROVIDER_TOKEN" not in hint
    assert portal.requests == []


@pytest.mark.parametrize("signed_in", SIGNED_IN_ELSEWHERE)
@pytest.mark.parametrize("path", sorted(WALLET_SIGNED))
def test_wallet_signed_commands_keep_the_old_line_in_plain_text(portal, path, signed_in) -> None:
    _save_session("ops@example.com")
    args = [*path, "x@example.com"] if path[-1] == "set-email" else [*path, "--password", "pw-123456"]
    result = _run(portal, "-y", *args, env=signed_in)
    assert result.exit_code == 1, result.output
    assert result.stderr == ("[ARG_INVALID] config commands require --hotkey (or LIUM_PROVIDER_HOTKEY)\n"
                             "  hint: Check the argument value and consult --help.\n")
    assert portal.requests == []
