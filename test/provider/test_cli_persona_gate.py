"""Tests for the persona-confirmation gate (A4)."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from lium.cli.provider._persona import (
    ConfirmationRequired,
    PersonaContext,
    confirm_persona,
    is_acked,
    mark_acked,
)
from ._agent_mode import AGENT_SWITCHES, read_error


def _persona() -> PersonaContext:
    return PersonaContext(coldkey="default", hotkey="hk1", shell_session_id="123")


def test_yes_flag_short_circuits_prompt(tmp_path: Path) -> None:
    @click.command()
    @click.pass_context
    def cmd(ctx):
        ok = confirm_persona(
            ctx,
            coldkey="default",
            hotkey="hk1",
            yes_flag=True,
            env={},
            path=tmp_path / "ack.json",
        )
        click.echo("ok" if ok else "no")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "ok"


def test_env_var_short_circuits_prompt(tmp_path: Path) -> None:
    @click.command()
    @click.pass_context
    def cmd(ctx):
        ok = confirm_persona(
            ctx,
            coldkey="default",
            hotkey="hk1",
            env={"LIUM_PROVIDER_ACK": "1"},
            path=tmp_path / "ack.json",
        )
        click.echo("ok" if ok else "no")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "ok"


def test_persisted_ack_skips_prompt_in_same_shell(tmp_path: Path) -> None:
    ack_path = tmp_path / "ack.json"
    persona = _persona()
    mark_acked(persona, path=ack_path)
    assert is_acked(persona, env={}, path=ack_path)


def test_user_typing_y_persists_ack(tmp_path: Path) -> None:
    ack_path = tmp_path / "ack.json"
    persona = _persona()

    @click.command()
    @click.pass_context
    def cmd(ctx):
        ok = confirm_persona(
            ctx,
            coldkey=persona.coldkey,
            hotkey=persona.hotkey,
            env={},
            path=ack_path,
            input_func=lambda: "y",
            output_func=lambda m: None,
        )
        click.echo("ok" if ok else "no")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "ok"
    # The ack key uses the *current* process's parent pid (per shell_session_id())
    # so our pre-built persona may have a different key. Confirm at least one
    # entry was written.
    import json

    assert ack_path.exists()
    data = json.loads(ack_path.read_text())
    assert isinstance(data, dict) and len(data) >= 1


def test_user_typing_anything_else_rejects(tmp_path: Path) -> None:
    @click.command()
    @click.pass_context
    def cmd(ctx):
        ok = confirm_persona(
            ctx,
            coldkey="default",
            hotkey="hk1",
            env={},
            path=tmp_path / "ack.json",
            input_func=lambda: "n",
            output_func=lambda m: None,
        )
        click.echo("ok" if ok else "no")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "no"


def test_corrupt_ack_file_does_not_crash(tmp_path: Path) -> None:
    ack_path = tmp_path / "ack.json"
    ack_path.write_text("not json{{{")
    persona = _persona()
    assert is_acked(persona, env={}, path=ack_path) is False


# --- text mode prompts as it always has; agent mode never reads stdin ------------


def _gate(tmp_path: Path, **kwargs):
    """Run the gate inside a click command; return (result, what confirm_persona gave or raised)."""
    seen: dict = {}

    @click.command()
    @click.pass_context
    def cmd(ctx):
        try:
            seen["ok"] = confirm_persona(ctx, coldkey="default", hotkey="hk1", env={}, path=tmp_path / "ack.json", **kwargs)
        except ConfirmationRequired as e:
            seen["raised"] = str(e)

    return CliRunner().invoke(cmd, []), seen


def test_json_mode_never_reads_stdin_it_asks_for_confirmation(tmp_path: Path) -> None:
    asked = []
    result, seen = _gate(tmp_path, json_mode=True, input_func=lambda: asked.append(1) or "y")
    assert result.exit_code == 0, result.output
    assert "--json" in seen["raised"] and asked == []


def test_lium_noninteractive_is_agent_mode_too(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    asked = []
    result, seen = _gate(tmp_path, input_func=lambda: asked.append(1) or "y")
    assert "LIUM_NONINTERACTIVE" in seen["raised"] and asked == []


def test_ctrl_c_at_the_text_prompt_is_a_decline(tmp_path: Path) -> None:
    def ctrl_c():
        raise KeyboardInterrupt

    result, seen = _gate(tmp_path, input_func=ctrl_c, output_func=lambda m: None)
    assert seen == {"ok": False}


def test_the_agent_mode_codes_have_no_legacy_code_and_exit_2_or_130() -> None:
    from lium.cli.provider._render import exit_code_for, legacy_code_for
    from lium.provider.errors import CONFIRMATION_REQUIRED, INTERRUPTED, ProviderError

    for code, exit_code in ((CONFIRMATION_REQUIRED, 2), (INTERRUPTED, 130)):
        err = ProviderError("x", code=code)
        assert (exit_code_for(err), exit_code_for(err, json_mode=True)) == (exit_code, exit_code), code
        assert legacy_code_for(err) is None


class _RecordingPortal:
    def __init__(self, on_delete=None):
        self.deletes: list = []
        self.on_delete = on_delete

    def delete(self, path, *, auth=True):
        if self.on_delete:
            self.on_delete()
        self.deletes.append(path)
        return {}


def _node_rm(monkeypatch, fake_signer, tmp_token_store, args, env=None, input=None, on_delete=None):
    from lium.cli.provider.command import provider_command
    from lium.provider.client import ProviderClient

    portal = _RecordingPortal(on_delete)
    monkeypatch.setattr(
        "lium.cli.provider.node.build_client",
        lambda ctx: ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal),
    )
    monkeypatch.setattr("lium.cli.provider._persona.DEFAULT_ACK_PATH", tmp_token_store.path.parent / "ack.json")
    env = {"LIUM_PROVIDER_ACK": "", "LIUM_OUTPUT": "", "LIUM_NONINTERACTIVE": "", **(env or {})}
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", *args], env=env, input=input)
    return result, portal


def test_text_mode_a_piped_y_confirms_and_the_command_runs(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], input="y\n")
    assert result.exit_code == 0, result.output
    assert portal.deletes == ["/executors/e-1"]


def test_text_mode_a_piped_n_declines_and_exits_1(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], input="n\n")
    assert result.exit_code == 1, result.output
    assert "[ARG_INVALID] persona confirmation declined" in result.stderr
    assert portal.deletes == []


def test_text_mode_eof_at_the_prompt_exits_1(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], input="")
    assert result.exit_code == 1, result.output
    assert "input.confirmation_required" not in result.output
    assert portal.deletes == []


def test_json_mode_a_piped_y_is_confirmation_required_exit_2(monkeypatch, fake_signer, tmp_token_store) -> None:
    import json

    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1"], input="y\n")
    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["exit_code"], error["legacy_code"]) == ("input.confirmation_required", 2, None)
    assert "--yes" in error["hint"] and error["data"] == {"flag": "--yes", "env": "LIUM_PROVIDER_ACK=1"}
    assert portal.deletes == []


def test_lium_output_json_ignores_a_piped_y_as_json_does(monkeypatch, fake_signer, tmp_token_store) -> None:
    import json

    result, portal = _node_rm(
        monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], env={"LIUM_OUTPUT": "json"}, input="y\n"
    )
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "input.confirmation_required"
    assert portal.deletes == []


def test_lium_noninteractive_ignores_a_piped_y_in_text_output(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(
        monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], env={"LIUM_NONINTERACTIVE": "1"}, input="y\n"
    )
    assert result.exit_code == 2, result.output
    assert "[input.confirmation_required]" in result.stderr and portal.deletes == []


def test_yes_or_the_ack_env_still_goes_through(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1", "--yes"])
    assert result.exit_code == 0, result.output
    result, portal = _node_rm(
        monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1"], env={"LIUM_PROVIDER_ACK": "1"}
    )
    assert result.exit_code == 0, result.output
    assert portal.deletes == ["/executors/e-1"]


def _ctrl_c():
    raise KeyboardInterrupt


def test_ctrl_c_under_json_is_input_interrupted_exit_130(monkeypatch, fake_signer, tmp_token_store) -> None:
    import json

    result, _ = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1", "--yes"], on_delete=_ctrl_c)
    assert result.exit_code == 130, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["exit_code"], error["legacy_code"]) == ("input.interrupted", 130, None)


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_ctrl_c_is_input_interrupted_exit_130_under_every_agent_switch(monkeypatch, fake_signer, tmp_token_store,
                                                                       switch) -> None:
    flags, env = switch
    result, _ = _node_rm(monkeypatch, fake_signer, tmp_token_store, [*flags, "node", "rm", "e-1", "--yes"], env=env,
                         on_delete=_ctrl_c)
    assert result.exit_code == 130, result.output
    assert read_error(result, switch)[0] == "input.interrupted"


def test_ctrl_c_in_text_mode_aborts_with_exit_1_as_before(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, _ = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1", "--yes"], on_delete=_ctrl_c)
    assert result.exit_code == 1, result.output
    assert "input.interrupted" not in result.output
