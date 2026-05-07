"""Tests for the persona-confirmation gate (A4)."""

from __future__ import annotations

from pathlib import Path

import click
from click.testing import CliRunner

from lium.cli.miner._persona import (
    PersonaContext,
    confirm_persona,
    is_acked,
    mark_acked,
)


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
