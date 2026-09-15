"""CLI tests for ``lium provider {billing,collateral,reclaim,machine-request,machine}``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.cli.provider.queries import _machine_request_summary_line
from lium.provider.auth import LocalKeypairSigner
from lium.provider.errors import PORTAL_FORBIDDEN, ProviderAuthError
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


def test_billing_list_default(patched_build_client, fake_signer) -> None:
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
    # Global portal listing → scoped to the caller's hotkey by default.
    assert portal.gets[0][1] == {"miner_hotkey": fake_signer.ss58_address}


def test_billing_list_all_is_unfiltered(patched_build_client) -> None:
    portal = _Portal(get_body={"data": [{"id": "b-1"}], "total": 1})
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command, ["--hotkey", "hk1", "--json", "billing", "list", "--all"]
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/billing"
    assert portal.gets[0][1] is None


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


_AGGREGATE_FEED = {
    "tier": "aggregate",
    "open_requests": 3,
    "by_gpu_class": [
        {"machine_name": "NVIDIA H200", "open_requests": 2},
        {"machine_name": "RTX A4000", "open_requests": 1},
    ],
    "by_hourly_budget_band": [
        {"band": "<$1/h", "open_requests": 1},
        {"band": "$10+/h", "open_requests": 2},
    ],
}


def test_machine_request_list_renders_the_aggregate_tier(patched_build_client) -> None:
    # DAH-2269: without a validator-verified node the portal answers counts, not rows
    portal = _Portal(get_body=_AGGREGATE_FEED)
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "machine-request", "list"],
    )
    assert result.exit_code == 0, result.output
    assert portal.gets[0][0] == "/machine-requests"
    assert "3 open" in result.output
    # the portal answers the aggregate to no token / an expired one as well, so the hint names both remedies
    assert "portal login and a validator-verified node" in result.output
    assert "NVIDIA H200" in result.output and "$10+/h" in result.output


def test_machine_request_list_json_passes_the_aggregate_through(patched_build_client) -> None:
    portal = _Portal(get_body=_AGGREGATE_FEED)
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--json", "--hotkey", "hk1", "machine-request", "list"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"] == _AGGREGATE_FEED


def test_machine_request_summary_line_counts_rows_when_the_feed_has_them() -> None:
    # negative control for the aggregate branch: a row list keeps the old "machine requests: N" line
    assert _machine_request_summary_line([{"id": "r-1"}, {"id": "r-2"}]) == "machine requests: 2"
    assert _machine_request_summary_line({"data": [{"id": "r-1"}], "total": 1}) == "machine requests: 1"
    assert "aggregate" in _machine_request_summary_line(_AGGREGATE_FEED)


def test_machine_request_get_forbidden_exits_2_without_telling_the_user_to_re_login(
    patched_build_client,
) -> None:
    portal = _Portal(
        get_raises=ProviderAuthError("portal forbade the requested action", code=PORTAL_FORBIDDEN)
    )
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "machine-request", "get", "r-1"],
    )
    assert result.exit_code == 2
    assert "PORTAL_FORBIDDEN" in result.output
    assert "validator-verified node" in result.output
    assert "Re-login" not in result.output


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
