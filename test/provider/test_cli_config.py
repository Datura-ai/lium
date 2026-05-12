"""CLI tests for ``lium provider config …`` (account configuration)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(self, *, get_body=None, post_body=None, post_raises=None):
        self._get_body = get_body
        self._post_body = post_body
        self._post_raises = post_raises
        self.posts: list[Any] = []
        self.gets: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        return self._get_body or {}

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        if self._post_raises:
            raise self._post_raises
        return self._post_body or {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, *a, **k):  # pragma: no cover
        return {}


@pytest.fixture
def patched_build_client(
    monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
):
    def _factory(portal: _Portal):
        def _builder(ctx):
            return ProviderClient(
                signer=fake_signer,
                token_store=tmp_token_store,
                http=portal,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("lium.cli.provider.config.build_client", _builder)
        return portal

    return _factory


def test_config_show(patched_build_client) -> None:
    portal = _Portal(
        get_body={
            "miner_hotkey": "5Foo",
            "miner_uid": 7,
            "email": "a@b.co",
            "opt_in_status": True,
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "config", "show"],
    )
    assert result.exit_code == 0, result.output


def test_config_opt_in_renders_central_endpoints(patched_build_client) -> None:
    portal = _Portal(
        post_body={
            "miner_hotkey": "5Foo",
            "miner_coldkey": "5Bar",
            "central_miner_ip": "1.2.3.4",
            "central_miner_port": 9090,
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "opt-in"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0] == ("/miners/opt-in", {"opt_in_status": True}, True)


def test_config_opt_out_sends_false(patched_build_client) -> None:
    portal = _Portal(
        post_body={
            "miner_hotkey": "5Foo",
            "miner_coldkey": "5Bar",
            "central_miner_ip": "0.0.0.0",
            "central_miner_port": 0,
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "opt-out"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][1] == {"opt_in_status": False}


def test_config_opt_in_json_envelope(patched_build_client) -> None:
    portal = _Portal(
        post_body={
            "miner_hotkey": "5Foo",
            "miner_coldkey": "5Bar",
            "central_miner_ip": "1.2.3.4",
            "central_miner_port": 9090,
        }
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "--json", "config", "opt-in"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["central_miner_ip"] == "1.2.3.4"


def test_config_set_email(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "set-email", "x@y.co"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/auth/set-email"
    assert portal.posts[0][1] == {"email": "x@y.co"}


def test_config_set_email_rejects_invalid(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "set-email", "not-an-email"],
    )
    assert result.exit_code != 0, result.output
    assert portal.posts == []


def test_config_set_subscriptions_multiple(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "-y",
            "--hotkey",
            "hk1",
            "config",
            "set-subscriptions",
            "--gpu",
            "H100",
            "--gpu",
            "RTX 4090",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/auth/set-machine-request-subscription"
    assert portal.posts[0][1] == {"machine_request_subscription": ["H100", "RTX 4090"]}


def test_config_set_subscriptions_empty_clears(patched_build_client) -> None:
    portal = _Portal(post_body={"data": {}})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "set-subscriptions"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][1] == {"machine_request_subscription": []}


def test_config_opt_in_requires_hotkey() -> None:
    runner = CliRunner()
    result = runner.invoke(provider_command, ["config", "opt-in"])
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output


def test_config_opt_in_passes_through_provider_error(patched_build_client) -> None:
    portal = _Portal(
        post_raises=ProviderError("server boom", code="PORTAL_SERVER_ERROR")
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "config", "opt-in"],
    )
    assert result.exit_code == 3, result.output
