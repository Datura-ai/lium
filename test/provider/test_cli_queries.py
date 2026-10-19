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
    def __init__(self, *, get_body=None, get_raises=None):
        self._get_body = get_body
        self._get_raises = get_raises
        self.gets: list[Any] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, params, auth))
        if self._get_raises is not None:
            raise self._get_raises
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


def test_billing_list_reads_the_signed_in_providers_daily_ledger(patched_build_client) -> None:
    rows = [{"date": "2026-10-16", "kind": "rental_share", "amount_usd": 4.5}] * 3
    # the transport shape: portal_http unwraps the DetailResponse, so `rows` is top level
    portal = _Portal(get_body={"date_from": "2026-10-01", "date_to": "2026-10-17", "summary": {}, "rows": rows})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(provider_command, ["--hotkey", "hk1", "billing", "list", "--from", "2026-10-01", "--to", "2026-10-17"])
    assert result.exit_code == 0, result.output
    assert "ledger rows: 3" in result.output
    assert result.output.count("rental_share") == 3
    assert portal.gets[0][:2] == ("/provider-ledger/daily", {"from": "2026-10-01", "to": "2026-10-17"})


@pytest.mark.parametrize(
    "window",
    [["--from", "2026-13-01"], ["--to", "17-10-2026"], ["--from", "2026-10-17", "--to", "2026-10-01"], ["--from", "2026-01-01", "--to", "2026-10-17"]],
)
def test_billing_list_refuses_a_bad_window_before_the_request(patched_build_client, window) -> None:
    portal = _Portal(get_body={"rows": []})
    patched_build_client(portal)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "billing", "list", *window])
    assert result.exit_code != 0 and portal.gets == []


@pytest.mark.parametrize("flag", [["--all"], ["--miner-hotkey", "5Foo"], ["--page", "2"]])
def test_billing_list_no_longer_reads_other_providers(patched_build_client, flag) -> None:
    portal = _Portal(get_body={"data": []})
    patched_build_client(portal)
    result = CliRunner().invoke(provider_command, ["--hotkey", "hk1", "billing", "list", *flag])
    assert result.exit_code == 2 and portal.gets == []


# ---------------------------------------------------------------------------
# Machine requests


# ---------------------------------------------------------------------------
# Machines


