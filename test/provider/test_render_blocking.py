"""Red BLOCKING panels in ``lium provider node list|get|status`` and ``lium provider status``.

The portal's ``blocking_reasons`` are rendered as they come; without them the list is rebuilt from
``computed_status.last_error``, ``hidden_reasons`` and the idle-pay reasons of ``GET /miners/overview``.
``--json`` carries the same list per node.
"""

from __future__ import annotations

import copy
import json
import re

import pytest
from click.testing import CliRunner

from lium.cli.provider._blocking import SECURE_GATING_CODES, fallback_reasons
from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.token_store import TokenStore


def _node(node_id="e-1", *, status="AVAILABLE", **extra):
    return {
        "id": node_id,
        "gpu_type": "H100",
        "gpu_count": 8,
        "executor_ip_address": "203.0.113.10",
        "executor_ip_port": "8080",
        "price_per_gpu": 2.5,
        "rented": False,
        "tier": "secure",
        "computed_status": {"status": status, "message": None, "last_error": None},
        "hidden_reasons": [],
        **extra,
    }


DRIVER_OVERVIEW = {
    "data": {
        "node_rows": [
            {
                "executor_id": "e-1",
                "idle_pay": "not_paid",
                "idle_pay_reasons": [
                    {
                        "code": "nvidia_driver_below_minimum",
                        "context": {"nvidia_driver_version": "550.54.15", "driver_multiplier": 0.0},
                        "message": "No unrented incentive: NVIDIA driver 550.54.15 is below the minimum version.",
                    }
                ],
            }
        ]
    }
}

PORTAL_REASONS = [
    {
        "code": "nvidia_driver_below_minimum",
        "message": "NVIDIA driver below the minimum",
        "measured": "550.54.15",
        "required": "580.65.06",
        "fix": "Upgrade the NVIDIA driver to 580.65.06 and restart the executor",
    },
    {
        "code": "insufficient_disk_for_vram",
        "message": "Not enough disk for the VRAM",
        "measured": "500 GB",
        "required": "1280 GB",
        "fix": "Grow the disk to 1280 GB",
    },
    {
        "code": "DISK_TOO_FULL",
        "message": "Disk 95% used",
        "measured": "95%",
        "required": "90% or less",
        "fix": "Free disk space",
        "secure": False,
    },
]


class _Portal:
    def __init__(self, *, nodes=(), node=None, overview=None, verification=None, me=None):
        self.nodes = list(nodes)
        self.node = node
        self.overview = overview if overview is not None else {"data": {"node_rows": []}}
        self.verification = verification or {"phase": "idle", "steps": [], "run": None, "last_run": None}
        self.me = me or {"provider_id": "p-1", "discord_id": "d-1"}
        self.gets: list[str] = []

    def get(self, path, *, params=None, auth=True):
        self.gets.append(path)
        if path == "/executors":
            return {"data": copy.deepcopy(self.nodes), "total": len(self.nodes), "page": 1, "limit": 20}
        if path == "/miners/overview":
            return copy.deepcopy(self.overview)
        if path.endswith("/verification"):
            return copy.deepcopy(self.verification)
        if path == "/auth/me":
            return dict(self.me)
        if path.startswith("/executors/"):
            return copy.deepcopy(self.node)
        return {}

    def post(self, *a, **k):  # pragma: no cover
        return {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, *a, **k):  # pragma: no cover
        return {}


@pytest.fixture
def portal_for(monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore):
    def _factory(portal: _Portal) -> _Portal:
        def _build(ctx):
            return ProviderClient(signer=fake_signer, token_store=tmp_token_store, http=portal)  # type: ignore[arg-type]

        monkeypatch.setattr("lium.cli.provider.node.build_client", _build)
        monkeypatch.setattr("lium.cli.provider.status.build_client", _build)
        return portal

    return _factory


def _flat(output: str) -> str:
    """Panel text on one line: borders dropped, wrapped lines joined."""
    return re.sub(r"\s+", " ", re.sub(r"[│╭╮╰╯─]", " ", output))


def _run(*args: str):
    return CliRunner().invoke(provider_command, ["--hotkey", "hk1", *args])


def test_node_list_driver_below_minimum_prints_a_blocking_panel_with_the_fix(portal_for):
    portal_for(_Portal(nodes=[_node("e-1"), _node("e-2")], overview=DRIVER_OVERVIEW))

    result = _run("node", "list")

    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "blocked=1" in text
    assert text.count("BLOCKING") == 1
    assert "BLOCKING e-1 · 8×H100 · 203.0.113.10:8080" in text
    assert "✗ NVIDIA driver below the network minimum" in text
    assert "measured 550.54.15 · required 580.65.06 or newer" in text
    assert "Fix: Upgrade the NVIDIA driver on this node to 580.65.06 or newer, reboot, then restart the executor" in text
    assert "Secure listing: 1 unmet requirement" in text
    assert re.search(r"1\s+BLOCKED\s+e-1", result.output)
    assert re.search(r"2\s+AVAILABLE\s+e-2", result.output)


def test_node_get_renders_every_portal_reason_and_the_unmet_secure_requirements(portal_for):
    portal = portal_for(_Portal(node=_node(blocking_reasons=PORTAL_REASONS)))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    assert "/miners/overview" not in portal.gets   # the portal's list needs no fallback
    text = _flat(result.output)
    assert "✗ NVIDIA driver below the minimum measured 550.54.15 · required 580.65.06" in text
    assert "Fix: Upgrade the NVIDIA driver to 580.65.06 and restart the executor" in text
    assert "✗ Not enough disk for the VRAM measured 500 GB · required 1280 GB Fix: Grow the disk to 1280 GB" in text
    assert "✗ Disk 95% used measured 95% · required 90% or less Fix: Free disk space" in text
    assert "Secure listing: 2 unmet requirements" in text
    secure_block = text.split("Secure listing:")[1]
    assert "• NVIDIA driver below the minimum" in secure_block
    assert "• Not enough disk for the VRAM" in secure_block
    assert "• Disk 95% used" not in secure_block
    assert "Blocking Reasons" not in text   # the panel prints them, not the key/value table


def test_fallback_reads_last_error_hidden_reasons_and_gating_idle_pay_reasons(portal_for):
    node = _node(
        status="VALIDATION_FAILED",
        computed_status={
            "status": "VALIDATION_FAILED",
            "message": "Validation failed",
            "last_error": {
                "title": "Network too slow",
                "message": "Upload 40 Mbit/s",
                "reason_code": "VERIFYX_FAILED_NETWORK_SPEED_TOO_SLOW",
                "remediation": "Move the node to a faster uplink",
                "source": "Validator",
            },
        },
        hidden_reasons=[
            {"code": "NEW_RENTALS_PAUSED", "message": "You paused new rentals"},
            {"code": "DISK_TOO_FULL", "message": "Hidden from renters: disk 95% used"},
        ],
    )
    overview = {
        "node_rows": [
            {
                "executor_id": "e-1",
                "idle_pay_reasons": [
                    {"code": "gpu_model_not_eligible_for_unrented_incentive", "context": {}},
                    {
                        "code": "price_above_market_p90_soft_limit",
                        "context": {"price_per_gpu": 3.2, "soft_limit_threshold": 2.4},
                    },
                ],
            }
        ]
    }
    portal_for(_Portal(node=node, overview=overview))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "✗ Network too slow Fix: Move the node to a faster uplink" in text
    assert "✗ Hidden from renters: disk 95% used Fix: Free disk on the node until it is at most 90% used." in text
    assert "✗ Price above the market's soft limit measured $3.2/GPU·h · required $2.4/GPU·h or less" in text
    assert "Fix: `lium provider node update-price e-1 --price 2.4`" in text
    assert "paused new rentals" not in text   # the provider's own choice, not a blocker
    assert "gpu_model_not_eligible" not in text   # the GPU program's scope is not gated
    assert "Secure listing: 1 unmet requirement" in text


def test_node_list_json_carries_the_same_list(portal_for):
    portal_for(
        _Portal(
            nodes=[_node("e-1"), _node("e-2", blocking_reasons=PORTAL_REASONS[:1]), _node("e-3")],
            overview=DRIVER_OVERVIEW,
        )
    )

    result = _run("--json", "node", "list")

    assert result.exit_code == 0, result.output
    rows = {row["id"]: row for row in json.loads(result.output)["data"]["data"]}
    assert rows["e-1"]["blocking_reasons_source"] == "cli_fallback"
    assert rows["e-1"]["blocking_reasons"] == [
        {
            "code": "nvidia_driver_below_minimum",
            "title": "NVIDIA driver below the network minimum",
            "measured": "550.54.15",
            "required": "580.65.06 or newer",
            "fix": (
                "Upgrade the NVIDIA driver on this node to 580.65.06 or newer, reboot, "
                "then restart the executor (`docker compose up -d` in neurons/executor)."
            ),
            "secure": True,
            "source": "idle_pay",
        }
    ]
    assert rows["e-2"]["blocking_reasons"] == PORTAL_REASONS[:1]   # the portal's list, untouched
    assert "blocking_reasons_source" not in rows["e-2"]
    assert rows["e-3"]["blocking_reasons"] == []


def test_node_list_all_prints_no_panels(portal_for):
    portal = portal_for(_Portal(nodes=[_node("e-1")], overview=DRIVER_OVERVIEW))

    result = _run("node", "list", "--all")

    assert result.exit_code == 0, result.output
    assert "BLOCKING" not in result.output
    assert "/miners/overview" not in portal.gets


def test_provider_status_counts_blocked_nodes_at_the_top(portal_for, monkeypatch):
    monkeypatch.setattr("lium.provider.client._read_metagraph", lambda **kw: (True, []))
    portal_for(_Portal(nodes=[_node("e-1"), _node("e-2")], overview=DRIVER_OVERVIEW))

    result = _run("status")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert "blocked=1" in lines[0]
    assert lines[1].startswith("✗ 1 of 2 nodes BLOCKED")
    text = _flat(result.output)
    assert "BLOCKING e-1" in text
    assert "Fix: Upgrade the NVIDIA driver on this node to 580.65.06 or newer" in text


def test_provider_status_json_carries_the_count_and_the_list(portal_for, monkeypatch):
    monkeypatch.setattr("lium.provider.client._read_metagraph", lambda **kw: (True, []))
    portal_for(_Portal(nodes=[_node("e-1"), _node("e-2")], overview=DRIVER_OVERVIEW))

    result = _run("--json", "status")

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["blocked_node_count"] == 1
    nodes = {n["id"]: n for n in data["nodes"]}
    assert [r["code"] for r in nodes["e-1"]["blocking_reasons"]] == ["nvidia_driver_below_minimum"]
    assert nodes["e-2"]["blocking_reasons"] == []


def test_node_status_prints_the_panel_and_json_carries_the_list(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=PORTAL_REASONS[:1])))

    result = _run("node", "status", "e-1")
    assert result.exit_code == 0, result.output
    assert "Fix: Upgrade the NVIDIA driver to 580.65.06 and restart the executor" in _flat(result.output)

    result = _run("--json", "node", "status", "e-1")
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["blocking_reasons"] == PORTAL_REASONS[:1]


def test_healthy_node_has_no_panel(portal_for):
    portal_for(_Portal(node=_node()))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    assert "BLOCKING" not in result.output


def test_secure_gating_codes_are_the_validators_node_level_codes():
    assert "gpu_model_not_eligible_for_unrented_incentive" not in SECURE_GATING_CODES
    assert "no_unrented_capacity_for_gpu_count" not in SECURE_GATING_CODES
    assert len(SECURE_GATING_CODES) == 11
    # a healthy status ignores a stale last_error
    row = _node(computed_status={"status": "AVAILABLE", "last_error": {"title": "old", "remediation": "x"}})
    assert fallback_reasons(row) == []
