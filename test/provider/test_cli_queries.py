"""CLI tests for ``lium provider {billing,collateral,reclaim,machine-request,machine}``."""

from __future__ import annotations

from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.token_store import TokenStore


class _Portal:
    def __init__(self, *, get_body=None):
        self._get_body = get_body
        self.gets: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        return self._get_body if self._get_body is not None else {}

    def post(self, *a, **k):  # pragma: no cover
        return {}

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

        monkeypatch.setattr("lium.cli.provider.queries.build_client", _builder)
        return portal

    return _factory


# ---------------------------------------------------------------------------
# Billing


def test_billing_list_default(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": 1}, {"id": 2}, {"id": 3}], "total": 3})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "billing", "list"],
    )
    assert result.exit_code == 0, result.output
    assert "billing entries: 3" in result.output
    assert portal.gets[0][0] == "/billing"


def test_billing_list_by_miner_uses_path_form(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [], "total": 0})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "billing",
            "list",
            "--miner-hotkey",
            "5Foo",
        ],
    )
    assert result.exit_code == 0, result.output
    # When only miner_hotkey is given (no page/limit), use path form.
    assert portal.gets[0][0] == "/billing/5Foo"


def test_billing_list_with_pagination_uses_query(patched_build_client) -> None:
    portal = _Portal(get_body={"data": []})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "billing",
            "list",
            "--miner-hotkey",
            "5Foo",
            "--page",
            "1",
            "--limit",
            "10",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/billing"
    assert portal.gets[0][1] == {"miner_hotkey": "5Foo", "page": 1, "limit": 10}


# ---------------------------------------------------------------------------
# Machine requests


def test_machine_request_list(patched_build_client) -> None:
    portal = _Portal(get_body=[{"id": "r-1"}])
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "machine-request", "list"],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/machine-requests"


def test_machine_request_get(patched_build_client) -> None:
    portal = _Portal(get_body={"id": "r-1"})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "machine-request", "get", "r-1"],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/machine-requests/r-1"


# ---------------------------------------------------------------------------
# Machines


def test_machine_list(patched_build_client) -> None:
    portal = _Portal(get_body=[{"name": "H100"}, {"name": "RTX 4090"}])
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "machine", "list"],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/machines"


def test_machine_estimate_passes_query_params(patched_build_client) -> None:
    portal = _Portal(get_body={"rewards_on_subnet": 0})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "machine",
            "estimate",
            "--gpu-type",
            "NVIDIA H200 NVL",
            "--gpu-count",
            "8",
            "--gpu-price",
            "3.5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/machines/estimated-rewards"
    assert portal.gets[0][1] == {
        "gpu_type": "NVIDIA H200 NVL",
        "gpu_count": 8,
        "gpu_price": 3.5,
    }


def test_machine_estimate_omits_optional_price(patched_build_client) -> None:
    portal = _Portal(get_body={"rewards_on_subnet": 0})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        [
            "--hotkey",
            "hk1",
            "machine",
            "estimate",
            "--gpu-type",
            "H100",
            "--gpu-count",
            "1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][1] == {"gpu_type": "H100", "gpu_count": 1}


def test_billing_requires_hotkey(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(provider_command, ["billing", "list"])
    assert result.exit_code == 1, result.output
