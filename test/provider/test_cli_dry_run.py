"""``lium provider --dry-run`` stops every portal write before a client is built."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command


@pytest.fixture
def no_client(monkeypatch):
    built: list[str] = []

    def _builder(ctx):
        built.append(ctx.command_path)
        raise AssertionError("dry run built a portal client")

    for module in ("node", "config", "sync"):
        monkeypatch.setattr(f"lium.cli.provider.{module}.build_client", _builder)
    return built


@pytest.mark.parametrize(
    "args",
    [
        ["--dry-run", "-y", "--hotkey", "hk1", "node", "rm", "abc-node"],
        ["-y", "--hotkey", "hk1", "node", "update-price", "abc-node", "--price", "0.01", "--dry-run"],
        ["--dry-run", "--hotkey", "hk1", "config", "opt-out"],
        ["--dry-run", "--hotkey", "hk1", "sync", "to-miner-server"],
        ["--dry-run", "--hotkey", "hk1", "config", "connect-discord", "--no-wait"],
    ],
)
def test_dry_run_sends_no_write(no_client, args) -> None:
    result = CliRunner().invoke(provider_command, args)

    assert result.exit_code == 0, result.output
    assert no_client == []
    assert "dry run:" in result.output
    assert "not sent" in result.output


def test_dry_run_names_the_target_of_the_write(no_client) -> None:
    result = CliRunner().invoke(provider_command, ["--dry-run", "--hotkey", "hk1", "node", "rm", "abc-node"])

    assert "node rm" in result.output
    assert "node_id='abc-node'" in result.output


def test_dry_run_set_password_neither_prompts_nor_echoes_the_password(no_client) -> None:
    result = CliRunner().invoke(
        provider_command,
        ["--dry-run", "--hotkey", "hk1", "config", "set-password", "--password", "s3cret-value"],
    )

    assert result.exit_code == 0, result.output
    assert no_client == []
    assert "s3cret-value" not in result.output


def test_dry_run_json_envelope_reports_intent(no_client) -> None:
    result = CliRunner().invoke(
        provider_command,
        ["--dry-run", "--json", "--hotkey", "hk1", "node", "update-price", "abc-node", "--price", "0.5"],
    )

    envelope = json.loads(result.output)
    assert envelope["ok"] is True
    assert envelope["data"]["dry_run"] is True
    assert envelope["data"]["params"]["node_id"] == "abc-node"
