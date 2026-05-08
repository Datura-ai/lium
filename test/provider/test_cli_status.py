"""CLI tests for ``lium provider status`` (M2)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(self, *, me_body=None, executors=None):
        self._me_body = me_body
        self._executors = executors

    def get(self, path, *, params=None, auth=True):
        if path == "/auth/me":
            return self._me_body or {}
        if path == "/executors":
            return {"data": self._executors or []}
        return {}

    def post(self, *a, **k):
        return {}

    def put(self, *a, **k):
        return {}

    def delete(self, *a, **k):
        return {}


@pytest.fixture
def patched_status(monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore):
    def _factory(*, me_body, executors, metagraph):
        portal = _Portal(me_body=me_body, executors=executors)

        original_status = ProviderClient.status

        def _patched_status(self, *, netuid: int = 51, metagraph_factory=None):
            return original_status(self, netuid=netuid, metagraph_factory=metagraph)

        monkeypatch.setattr(ProviderClient, "status", _patched_status)

        def _build(ctx):
            return ProviderClient(
                signer=fake_signer,
                token_store=tmp_token_store,
                http=portal,  # type: ignore[arg-type]
            )

        monkeypatch.setattr("lium.cli.provider.status.build_client", _build)
        return portal

    return _factory


def test_status_default_human_summary(patched_status, fake_signer) -> None:
    metagraph = lambda netuid: type("M", (), {  # noqa: E731
        "hotkeys": [fake_signer.ss58_address],
        "W": [[0.5]],
    })()
    patched_status(
        me_body={"provider": {"id": "m-1"}},
        executors=[{"id": "e-1", "gpu_count": 8}],
        metagraph=metagraph,
    )
    runner = CliRunner()
    result = runner.invoke(provider_command, ["--hotkey", "hk1", "status"])
    assert result.exit_code == 0, result.output
    assert "provider status:" in result.output
    assert "nodes=1" in result.output


def test_status_json_envelope(patched_status, fake_signer) -> None:
    metagraph = lambda netuid: type("M", (), {  # noqa: E731
        "hotkeys": [fake_signer.ss58_address],
        "W": [[0.0]],
    })()
    patched_status(
        me_body={"provider": {"id": "m-1"}},
        executors=[],
        metagraph=metagraph,
    )
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "--json", "status"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["provider_id"] == "m-1"
    assert payload["data"]["registered_on_subnet"] is True
    assert payload["data"]["node_count"] == 0


def test_status_persona_gate_not_required(patched_status, fake_signer) -> None:
    """``status`` is read-only and must not trigger the persona gate."""
    metagraph = lambda netuid: type("M", (), {"hotkeys": [], "W": []})()  # noqa: E731
    patched_status(me_body=None, executors=[], metagraph=metagraph)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "status"],
        env={"LIUM_PROVIDER_ACK": ""},
    )
    # Even without --yes / env var, status should run through.
    assert result.exit_code == 0, result.output
