"""CLI tests for ``lium provider sync …`` (batch executor sync).

The CLI verbs match the frontend's user-facing button labels:

- ``sync from-miner-server`` -> POST /executors/sync-executor-central-miner
- ``sync to-miner-server``   -> POST /executors/sync-executor-miner-portal

These pin the route-name <-> verb-name mapping so it cannot be silently
flipped by a future maintainer assuming "from" and "to" should match.
"""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(self, *, post_body=None, post_raises=None):
        self._post_body = post_body
        self._post_raises = post_raises
        self.posts: list[Any] = []

    def get(self, *a, **k):  # pragma: no cover
        return {}

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

        monkeypatch.setattr("lium.cli.provider.sync.build_client", _builder)
        return portal

    return _factory


def test_sync_from_miner_server(patched_build_client) -> None:
    portal = _Portal(post_body={"message": "queued"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "sync", "from-miner-server"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/sync-executor-central-miner"


def test_sync_to_miner_server(patched_build_client) -> None:
    portal = _Portal(post_body={"message": "queued"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "sync", "to-miner-server"],
    )
    assert result.exit_code == 0, result.output
    assert portal.posts[0][0] == "/executors/sync-executor-miner-portal"


def test_sync_requires_hotkey() -> None:
    runner = CliRunner()
    result = runner.invoke(provider_command, ["sync", "from-miner-server"])
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output


def test_sync_provider_error_exit_3(patched_build_client) -> None:
    portal = _Portal(post_raises=ProviderError("boom", code="PORTAL_SERVER_ERROR"))
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["-y", "--hotkey", "hk1", "sync", "from-miner-server"],
    )
    assert result.exit_code == 3, result.output
