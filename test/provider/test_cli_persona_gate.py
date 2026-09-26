"""Tests for the persona-confirmation gate (A4)."""

from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from lium.cli.provider._persona import (
    ConfirmationRequired,
    PersonaContext,
    ack_scope,
    confirm_persona,
    is_acked,
    mark_acked,
)


def _persona() -> PersonaContext:
    return PersonaContext(coldkey="default", hotkey="hk1", scope="user:test")


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
            interactive=True,
            input_func=lambda: "y",
            output_func=lambda m: None,
        )
        click.echo("ok" if ok else "no")

    runner = CliRunner()
    result = runner.invoke(cmd, [])
    assert result.exit_code == 0
    assert result.output.strip() == "ok"
    import json

    data = json.loads(ack_path.read_text())
    assert list(data) == [f"default::hk1::{ack_scope()}"]


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
            interactive=True,
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


# --- no prompt without a person to answer it -------------------------------------


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


def test_json_mode_never_prompts_it_asks_for_confirmation(tmp_path: Path) -> None:
    asked = []
    result, seen = _gate(tmp_path, json_mode=True, interactive=True, input_func=lambda: asked.append(1) or "y")
    assert result.exit_code == 0, result.output
    assert "--json" in seen["raised"] and asked == []


def test_no_terminal_never_prompts_it_asks_for_confirmation(tmp_path: Path) -> None:
    asked = []
    result, seen = _gate(tmp_path, interactive=False, input_func=lambda: asked.append(1) or "y")
    assert "not a terminal" in seen["raised"] and asked == []


def test_the_default_reads_the_terminal_so_a_pipe_is_not_asked(tmp_path: Path) -> None:
    # CliRunner's stdin is not a terminal: with no `interactive=` the gate must not wait on it
    result, seen = _gate(tmp_path, input_func=lambda: "y")
    assert "raised" in seen, seen


def test_eof_at_the_prompt_is_no_answer_not_a_crash(tmp_path: Path) -> None:
    def eof():
        raise click.Abort()   # what click.prompt raises on EOF

    result, seen = _gate(tmp_path, interactive=True, input_func=eof, output_func=lambda m: None)
    assert result.exit_code == 0, result.output
    assert "stdin closed" in seen["raised"]


def test_an_ack_covers_every_process_of_the_user_not_one_parent_pid(tmp_path: Path, monkeypatch) -> None:
    import os

    monkeypatch.setattr(os, "getppid", lambda: 1111)
    result, seen = _gate(tmp_path, interactive=True, input_func=lambda: "y", output_func=lambda m: None)
    assert seen["ok"] is True
    # the next agent subprocess has another parent: no prompt, no refusal
    monkeypatch.setattr(os, "getppid", lambda: 2222)
    result, seen = _gate(tmp_path, interactive=False)
    assert seen == {"ok": True}


def test_the_ack_key_names_the_user() -> None:
    import getpass

    assert ack_scope() == f"user:{getpass.getuser()}"


class _RecordingPortal:
    def __init__(self):
        self.deletes: list = []

    def delete(self, path, *, auth=True):
        self.deletes.append(path)
        return {}


def _node_rm(monkeypatch, fake_signer, tmp_token_store, args, env=None):
    from lium.cli.provider.command import provider_command
    from lium.provider.client import ProviderClient

    portal = _RecordingPortal()
    monkeypatch.setattr(
        "lium.cli.provider.node.build_client",
        lambda ctx: ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal),
    )
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", *args], env={"LIUM_PROVIDER_ACK": "", **(env or {})})
    return result, portal


def test_a_mutation_under_json_without_yes_is_confirmation_required_exit_2(monkeypatch, fake_signer, tmp_token_store) -> None:
    import json

    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1"])
    assert result.exit_code == 2, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["exit_code"]) == ("input.confirmation_required", 2)
    assert "--yes" in error["hint"] and error["data"] == {"flag": "--yes", "env": "LIUM_PROVIDER_ACK=1"}
    assert portal.deletes == []


def test_a_mutation_behind_a_pipe_is_refused_in_text_mode_too(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], env={"LIUM_OUTPUT": ""})
    assert result.exit_code == 2, result.output
    assert "input.confirmation_required" in result.stderr
    assert portal.deletes == []


def test_lium_output_json_is_the_same_as_json(monkeypatch, fake_signer, tmp_token_store) -> None:
    import json

    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["node", "rm", "e-1"], env={"LIUM_OUTPUT": "json"})
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["error"]["code"] == "input.confirmation_required"


def test_yes_or_the_ack_env_still_goes_through(monkeypatch, fake_signer, tmp_token_store) -> None:
    result, portal = _node_rm(monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1", "--yes"])
    assert result.exit_code == 0, result.output
    result, portal = _node_rm(
        monkeypatch, fake_signer, tmp_token_store, ["--json", "node", "rm", "e-1"], env={"LIUM_PROVIDER_ACK": "1"}
    )
    assert result.exit_code == 0, result.output
    assert portal.deletes == ["/executors/e-1"]
