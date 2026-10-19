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
