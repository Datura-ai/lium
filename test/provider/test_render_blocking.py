"""Red BLOCKING panels in ``lium provider node list|get|status`` and ``lium provider status``.

The portal's ``blocking_reasons`` are rendered as they come; without them the list is rebuilt from
``computed_status.last_error``, ``hidden_reasons`` and the idle-pay reasons of ``GET /miners/overview``.
``--json`` carries the same list per node.
"""

from __future__ import annotations

import copy
import io
import json
import re

import pytest
from click.testing import CliRunner
from rich.console import Console

from lium.cli.provider import _blocking
from lium.cli.provider._blocking import LEGACY_GATING_CODES, fallback_reasons
from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderError
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
            "gating": True,
            "source": "idle_pay",
        }
    ]
    # the portal's list; an entry without `gating` (an older portal) gets the legacy verdict, marked
    assert rows["e-2"]["blocking_reasons"] == [{**PORTAL_REASONS[0], "gating": True, "gating_source": "cli_legacy_fallback"}]
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
    assert json.loads(result.output)["data"]["blocking_reasons"] == [{**PORTAL_REASONS[0], "gating": True, "gating_source": "cli_legacy_fallback"}]


def test_healthy_node_has_no_panel(portal_for):
    portal_for(_Portal(node=_node()))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    assert "BLOCKING" not in result.output


def test_secure_gating_codes_are_the_validators_node_level_codes():
    assert "gpu_model_not_eligible_for_unrented_incentive" not in LEGACY_GATING_CODES
    assert "no_unrented_capacity_for_gpu_count" not in LEGACY_GATING_CODES
    assert len(LEGACY_GATING_CODES) == 11
    # a healthy status ignores a stale last_error
    row = _node(computed_status={"status": "AVAILABLE", "last_error": {"title": "old", "remediation": "x"}})
    assert fallback_reasons(row) == []


NOT_ELIGIBLE_OVERVIEW = {
    "node_rows": [
        {
            "executor_id": "e-2",
            "idle_pay": "not_paid",
            "idle_pay_reasons": [
                {"code": "gpu_model_not_eligible_for_unrented_incentive", "context": {}, "message": None},
                {"code": "no_unrented_capacity_for_gpu_count", "context": {}, "message": None},
            ],
        }
    ]
}
GPU_LINE = "Not eligible for idle pay: this GPU model is not in the idle-pay program"
GPU_ACTION = "No action: this model earns from rentals only."
ROOM_LINE = "Not eligible for idle pay: no idle-pay room for this node size right now"
ROOM_ACTION = "No action: room for this size is full. Rentals still pay, and room opens as the market moves."


@pytest.fixture
def ansi_console(monkeypatch):
    """The panels and idle-pay lines printed with colour, so a test can tell red from not red."""
    for var in ("NO_COLOR", "FORCE_COLOR", "COLUMNS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")   # a dumb terminal would drop the colour and wrap at 80
    out = io.StringIO()
    monkeypatch.setattr(_blocking, "console", Console(file=out, force_terminal=True, color_system="standard", width=200))
    return out


def _red_lines(ansi: str) -> list[str]:
    return [line for line in ansi.splitlines() if re.search(r"\x1b\[[0-9;]*31m", line)]


def test_node_get_shows_both_not_eligible_lines_with_no_action_and_not_in_red(portal_for, ansi_console):
    portal_for(_Portal(node=_node("e-2"), overview=NOT_ELIGIBLE_OVERVIEW))

    result = _run("node", "get", "e-2")

    assert result.exit_code == 0, result.output
    ansi = ansi_console.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
    for line in (GPU_LINE, GPU_ACTION, ROOM_LINE, ROOM_ACTION):
        assert line in plain
    assert "BLOCKING" not in plain
    assert _red_lines(ansi) == []


def test_not_eligible_lines_sit_outside_a_blocking_panel_on_a_blocked_node(portal_for, ansi_console):
    overview = copy.deepcopy(DRIVER_OVERVIEW)
    overview["data"]["node_rows"][0]["idle_pay_reasons"].append(
        {"code": "gpu_model_not_eligible_for_unrented_incentive", "context": {}}
    )
    portal_for(_Portal(node=_node("e-1"), overview=overview))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    ansi = ansi_console.getvalue()
    red = "\n".join(_red_lines(ansi))
    assert "NVIDIA driver below the network minimum" in red
    assert "not in the idle-pay program" not in red
    assert GPU_LINE in re.sub(r"\x1b\[[0-9;]*m", "", ansi)
    panel = re.sub(r"\x1b\[[0-9;]*m", "", ansi).split("╰")[0]
    assert "idle-pay program" not in panel
    assert "Secure listing: 1 unmet requirement" in panel


def test_node_list_marks_a_not_eligible_node_without_counting_it_blocked(portal_for, ansi_console):
    portal_for(_Portal(nodes=[_node("e-1"), _node("e-2")], overview=NOT_ELIGIBLE_OVERVIEW))

    result = _run("node", "list")

    assert result.exit_code == 0, result.output
    assert "blocked=0" in result.output
    assert re.search(r"2\s+AVAILABLE\s+e-2", result.output)
    assert "BLOCKED" not in result.output
    ansi = ansi_console.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
    assert (
        "◦ e-2: not eligible for idle pay (this GPU model is not in the idle-pay program; "
        "no idle-pay room for this node size right now); no action needed"
    ) in plain
    assert "e-1" not in plain
    assert _red_lines(ansi) == []


def test_provider_status_shows_the_marker_and_counts_no_blocked_node(portal_for, monkeypatch, ansi_console):
    monkeypatch.setattr("lium.provider.client._read_metagraph", lambda **kw: (True, []))
    portal_for(_Portal(nodes=[_node("e-1"), _node("e-2")], overview=NOT_ELIGIBLE_OVERVIEW))

    result = _run("status")

    assert result.exit_code == 0, result.output
    assert "blocked=0" in result.output.splitlines()[0]
    assert "nodes BLOCKED" not in result.output
    assert "◦ e-2: not eligible for idle pay" in re.sub(r"\x1b\[[0-9;]*m", "", ansi_console.getvalue())
    assert _red_lines(ansi_console.getvalue()) == []

    result = _run("--json", "status")
    data = json.loads(result.output)["data"]
    assert data["blocked_node_count"] == 0
    e2 = next(n for n in data["nodes"] if n["id"] == "e-2")
    assert [(r["code"], r["gating"]) for r in e2["blocking_reasons"]] == [
        ("gpu_model_not_eligible_for_unrented_incentive", False),
        ("no_unrented_capacity_for_gpu_count", False),
    ]


def test_json_carries_the_not_eligible_reasons_with_gating_false(portal_for):
    portal_for(_Portal(node=_node("e-2"), overview=NOT_ELIGIBLE_OVERVIEW))

    result = _run("--json", "node", "get", "e-2")

    assert result.exit_code == 0, result.output
    reasons = json.loads(result.output)["data"]["blocking_reasons"]
    assert reasons[0] == {
        "code": "gpu_model_not_eligible_for_unrented_incentive",
        "title": "This GPU model is not in the idle-pay program",
        "measured": None,
        "required": None,
        "fix": GPU_ACTION,
        "secure": False,
        "gating": False,
        "source": "idle_pay",
    }
    assert reasons[1]["fix"] == ROOM_ACTION and reasons[1]["gating"] is False


def test_node_status_shows_a_portal_gating_false_entry_as_not_eligible(portal_for, ansi_console):
    served = [{"code": "gpu_model_not_eligible", "message": "This GPU model is not in the idle-pay program",
               "fix": GPU_ACTION, "gating": False}]
    portal_for(_Portal(node=_node("e-2", blocking_reasons=served)))

    result = _run("node", "status", "e-2")

    assert result.exit_code == 0, result.output
    ansi = ansi_console.getvalue()
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
    assert GPU_LINE in plain and GPU_ACTION in plain
    assert "BLOCKING" not in plain
    assert _red_lines(ansi) == []


# Entries as the portal's blocking-reasons catalog serves them: no `gating` key; `secure_requirement`
# comes from the portal's gating-codes setting.
CATALOG_DRIVER = {
    "code": "nvidia_driver_below_minimum", "docs_url": "https://docs.lium.io/providers/nodes/quickstart",
    "fix": "Upgrade the NVIDIA driver on the host to 580.65.06 or newer (on Ubuntu, for example `sudo apt-get install -y nvidia-driver-580`), reboot the host, then restart the executor. The next validator cycle checks it again; rented nodes are exempt until their rental ends.",
    "fix_command": "nvidia-smi --query-gpu=driver_version --format=csv,noheader && cd neurons/executor && docker compose up -d",
    "kind": "idle_pay", "measured": "550.54.15",
    "message": "NVIDIA driver 550.54.15 is below the network minimum 580.65.06: no idle pay.",
    "required": "580.65.06 or newer", "secure_requirement": True, "title": "NVIDIA driver below the minimum",
}
CATALOG_SYSBOX = {
    "code": "sysbox_not_enabled", "docs_url": "https://docs.lium.io/providers/nodes/sysbox",
    "fix": "Install sysbox with the Lium setup script (it checks the host first and prints the fix for each FIX line), then restart the executor.",
    "fix_command": "curl -fsSL https://raw.githubusercontent.com/Datura-ai/lium-io/main/neurons/executor/nvidia_docker_sysbox_setup.sh | sudo bash",
    "kind": "idle_pay", "measured": "no sysbox-runc runtime", "message": "The node does not run the sysbox runtime: no idle pay.",
    "required": "sysbox-runc runtime", "secure_requirement": True, "title": "Sysbox runtime missing",
}
CATALOG_GPU_MODEL = {
    "code": "gpu_model_not_eligible_for_unrented_incentive", "docs_url": None,
    "fix": "Nothing to fix on the node: rent it out to earn.", "fix_command": None, "kind": "idle_pay", "measured": None,
    "message": "This GPU model is not part of the idle-pay program: it earns only when rented.", "required": None,
    "secure_requirement": False, "title": "GPU model outside the idle-pay program",
}
CATALOG_NO_ROOM = {
    "code": "no_unrented_capacity_for_gpu_count", "docs_url": None,
    "fix": "Nothing to fix on the node: the fleet cap changes with the market. Rent it out to earn.", "fix_command": None,
    "kind": "idle_pay", "measured": None,
    "message": "The idle-pay program has no capacity for this GPU model and count this cycle: it earns only when rented.",
    "required": None, "secure_requirement": False, "title": "No idle-pay capacity for this GPU count",
}
# the price code taken out of the portal's gating-codes setting: still a reason, not a Secure requirement
CATALOG_PRICE_NOT_GATED = {
    "code": "price_above_market_p90_soft_limit", "docs_url": "https://docs.lium.io/providers/portal/managing-nodes",
    "fix": "Lower the node's price to $2.75/GPU/h or below on the node's page in the provider portal.", "fix_command": None,
    "kind": "idle_pay", "measured": "$3.2/GPU/h", "message": "The price ($3.2/GPU/h) is above the market soft limit: no idle pay.",
    "required": "at most $2.75/GPU/h", "secure_requirement": False, "title": "Price above the market soft limit",
}


def test_catalog_entries_print_fix_command_docs_link_and_secure_requirements(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[CATALOG_DRIVER, CATALOG_SYSBOX, CATALOG_PRICE_NOT_GATED])))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "✗ Sysbox runtime missing measured no sysbox-runc runtime · required sysbox-runc runtime" in text
    fix = text.index("Fix: Install sysbox with the Lium setup script")
    command = text.index("nvidia_docker_sysbox_setup.sh | sudo bash")
    docs = text.index("Docs: https://docs.lium.io/providers/nodes/sysbox")
    assert fix < command < docs
    assert "nvidia-smi --query-gpu=driver_version --format=csv,noheader && cd neurons/executor && docker compose up -d" in text
    assert "Docs: https://docs.lium.io/providers/nodes/quickstart" in text
    # secure_requirement decides, not the code: the price stays a reason but is no Secure requirement
    assert "✗ Price above the market soft limit" in text
    secure_block = text.split("Secure listing:")[1]
    assert secure_block.startswith(" 2 unmet requirements")
    assert "• Price above the market soft limit" not in secure_block


def test_catalog_command_sits_on_its_own_line_under_fix(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[CATALOG_DRIVER])))

    result = _run("node", "get", "e-1")

    lines = [re.sub(r"[│\s]+$", "", re.sub(r"^[│\s]+", "", line)) for line in result.output.splitlines()]
    assert "nvidia-smi --query-gpu=driver_version --format=csv,noheader && cd neurons/executor && docker compose up" in " ".join(lines)
    command_row = next(i for i, line in enumerate(lines) if line.startswith("nvidia-smi --query-gpu"))
    assert any(line.startswith("Fix: Upgrade the NVIDIA driver") for line in lines[:command_row])


def test_catalog_not_gated_entries_without_a_gating_key_are_not_eligible_not_blocked(portal_for, ansi_console):
    portal_for(_Portal(nodes=[_node("e-2", blocking_reasons=[CATALOG_GPU_MODEL, CATALOG_NO_ROOM])]))

    result = _run("node", "list")

    assert result.exit_code == 0, result.output
    assert "blocked=0" in result.output
    assert "BLOCKED" not in result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi_console.getvalue())
    assert "BLOCKING" not in plain
    # a title that opens with an acronym keeps its capitals
    assert (
        "◦ e-2: not eligible for idle pay (GPU model outside the idle-pay program; "
        "no idle-pay capacity for this GPU count); no action needed"
    ) in plain
    assert _red_lines(ansi_console.getvalue()) == []


def test_catalog_not_gated_entry_prints_its_title_as_sent_with_a_no_action_line(portal_for, ansi_console):
    portal_for(_Portal(node=_node("e-2", blocking_reasons=[CATALOG_GPU_MODEL])))

    result = _run("node", "get", "e-2")

    assert result.exit_code == 0, result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi_console.getvalue())
    assert "Not eligible for idle pay: GPU model outside the idle-pay program" in plain
    assert "gPU" not in plain
    assert GPU_ACTION in plain


def test_catalog_secure_requirement_on_a_spot_node_is_no_secure_listing_line(portal_for):
    portal_for(_Portal(node=_node(tier="spot", blocking_reasons=[CATALOG_DRIVER])))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "✗ NVIDIA driver below the minimum" in text
    assert "Secure listing" not in text


def test_node_list_miner_hotkey_own_reads_the_overview_and_another_does_not(portal_for):
    portal = portal_for(_Portal(nodes=[_node("e-1")], overview=DRIVER_OVERVIEW))

    result = _run("node", "list", "--miner-hotkey", "5FakeHotkey")   # the fake signer's own ss58
    assert result.exit_code == 0, result.output
    assert "/miners/overview" in portal.gets
    assert "blocked=1" in result.output
    assert "BLOCKING e-1" in _flat(result.output)

    portal.gets.clear()
    result = _run("node", "list", "--miner-hotkey", "5SomeoneElse")
    assert result.exit_code == 0, result.output
    assert "/miners/overview" not in portal.gets   # the overview is the signed-in provider's, not theirs
    assert "blocked=0" in result.output
    assert "BLOCKING" not in result.output


def test_a_healthy_partly_rented_node_is_not_blocked(portal_for):
    node = _node(
        "e-1",
        status="RENTED",
        rented=True,
        rented_gpu_count=4,
        listing_state="rented",
        hidden_reasons=[
            {"code": "WHOLE_HOST_ONLY", "message": "The free GPUs are not listed: this host rents only as a whole host"},
            {"code": "SPLIT_MINIMUM_NOT_MET", "message": "The free GPUs are below the node's minimum GPU count"},
        ],
    )
    portal_for(_Portal(nodes=[node], node=node))

    result = _run("node", "list")
    assert result.exit_code == 0, result.output
    assert "blocked=0" in result.output
    assert "BLOCKING" not in result.output
    assert re.search(r"1\s+RENTED\s+e-1", result.output)

    result = _run("--json", "node", "get", "e-1")
    assert json.loads(result.output)["data"]["blocking_reasons"] == []


# --- the agent contract: portal `gating`, the envelope, LIUM_OUTPUT, exit 10 -------------------------

DRIVER_GATING = {
    "kind": "idle_pay", "code": "nvidia_driver_below_minimum", "gating": True,
    "message": "NVIDIA driver below the minimum", "measured": "550.54.15", "required": "580.65.06 or newer",
    "fix": "Upgrade the NVIDIA driver on the host to 580.65.06 or newer, reboot the host, then restart the executor.",
    "fix_command": "sudo apt-get install -y nvidia-driver-580 && sudo reboot",
    "verify_command": "nvidia-smi --query-gpu=driver_version --format=csv,noheader",
    "requires": ["sudo", "reboot"], "docs_url": "https://docs.lium.io/providers/nodes/quickstart",
}


class _SequencePortal(_Portal):
    """``GET /executors/{id}`` answers with the next node of ``sequence`` (the last one repeats)."""

    def __init__(self, sequence, **kw):
        super().__init__(node=sequence[0], **kw)
        self.sequence = list(sequence)

    def get(self, path, *, params=None, auth=True):
        if path.startswith("/executors/") and not path.endswith("/verification"):
            self.gets.append(path)
            node = self.sequence.pop(0) if len(self.sequence) > 1 else self.sequence[0]
            return copy.deepcopy(node)
        return super().get(path, params=params, auth=auth)


class _FailingPortal(_Portal):
    def get(self, path, *, params=None, auth=True):
        raise ProviderError("no such node", code="PORTAL_NOT_FOUND")


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if len(self.sleeps) > 50:
            raise AssertionError("the watch never stopped")


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr("lium.cli.provider.node.time", fake)
    return fake


def _envelopes(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_the_portals_gating_decides_not_a_code_list_in_the_cli(portal_for):
    new_code = {"kind": "idle_pay", "code": "some_new_validator_check", "gating": True,
                "message": "A check the CLI has never heard of", "fix": "Do the new thing"}
    price_off = {"kind": "idle_pay", "code": "price_above_market_p90_soft_limit", "gating": False,
                 "message": "Price above the market soft limit", "fix": "No action: the portal does not gate on price"}
    portal_for(_Portal(node=_node(blocking_reasons=[new_code, price_off])))

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    text = _flat(result.output)
    assert "✗ A check the CLI has never heard of" in text
    assert "Secure listing: 1 unmet requirement" in text and "• A check the CLI has never heard of" in text
    assert "✗ Price above" not in text
    assert "Not eligible for idle pay: price above the market soft limit" in text

    result = _run("--json", "node", "get", "e-1")
    assert json.loads(result.stdout)["data"]["blocking_reasons"] == [new_code, price_off]   # served, untouched


def test_an_entry_without_gating_takes_the_legacy_fallback_and_says_so(portal_for):
    old_portal = {"code": "some_new_validator_check", "message": "A check the CLI has never heard of", "fix": "x"}
    portal_for(_Portal(node=_node(blocking_reasons=[old_portal])))

    result = _run("--json", "node", "get", "e-1")

    [entry] = json.loads(result.stdout)["data"]["blocking_reasons"]
    assert entry == {**old_portal, "gating": True, "gating_source": "cli_legacy_fallback"}
    text = _flat(_run("node", "get", "e-1").output)
    assert "✗ A check the CLI has never heard of" in text
    assert "Secure listing" not in text   # the legacy list does not know the code


def test_a_provider_error_is_the_namespaced_envelope_on_stdout(portal_for):
    portal_for(_FailingPortal())

    result = _run("--json", "node", "get", "e-1")

    assert result.exit_code == 3
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False,
        "error": {
            "code": "portal.not_found",
            "legacy_code": "PORTAL_NOT_FOUND",
            "message": "no such node",
            "hint": "The portal returned 404 for that resource (wrong UUID or already removed).",
            "exit_code": 3,
            "context": {},
        },
    }


def test_every_provider_error_code_is_namespaced_snake_case():
    from lium.cli.provider._render import ERROR_CODES, error_code_for
    from lium.provider import errors

    upper = {v for k, v in vars(errors).items() if k.isupper() and isinstance(v, str) and v.isupper() and "_" in v}
    assert upper <= set(ERROR_CODES)
    namespaces = {"auth", "input", "portal", "net", "ssh", "host", "node"}
    for code in upper:
        namespace, _, name = error_code_for(code).partition(".")
        assert namespace in namespaces and re.fullmatch(r"[a-z0-9_]+", name), code
    assert error_code_for("node.blocked.sysbox_not_enabled") == "node.blocked.sysbox_not_enabled"


def test_lium_output_json_is_the_same_as_the_json_flag(portal_for, monkeypatch):
    portal_for(_Portal(node=_node(blocking_reasons=[DRIVER_GATING])))
    monkeypatch.setenv("LIUM_OUTPUT", "json")

    result = _run("node", "get", "e-1")

    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True and envelope["data"]["id"] == "e-1"
    assert "BLOCKING" not in result.output

    portal_for(_FailingPortal())
    assert json.loads(_run("node", "get", "e-1").stdout)["error"]["code"] == "portal.not_found"


def test_node_get_fail_on_blocked_exits_10_with_one_envelope(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[DRIVER_GATING])))

    result = _run("--json", "node", "get", "e-1", "--fail-on-blocked")

    assert result.exit_code == 10
    [envelope] = _envelopes(result.stdout)
    assert envelope["ok"] is False
    error = envelope["error"]
    assert (error["code"], error["exit_code"]) == ("node.blocked.nvidia_driver_below_minimum", 10)
    assert error["hint"] == DRIVER_GATING["fix"]
    assert error["data"]["id"] == "e-1" and error["data"]["blocking_reasons"] == [DRIVER_GATING]

    result = _run("node", "get", "e-1", "--fail-on-blocked")
    assert result.exit_code == 10
    assert "BLOCKING e-1" in _flat(result.stdout)
    assert "[node.blocked.nvidia_driver_below_minimum] node e-1 is blocked" in result.stderr

    assert _run("node", "get", "e-1").exit_code == 0   # without the flag a blocked node still exits 0


def test_fail_on_blocked_passes_a_clear_or_only_not_eligible_node(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[CATALOG_GPU_MODEL])))
    assert _run("node", "get", "e-1", "--fail-on-blocked").exit_code == 0
    portal_for(_Portal(node=_node()))
    assert _run("--json", "node", "status", "e-1", "--fail-on-blocked").exit_code == 0


def test_node_status_fail_on_blocked_exits_10(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[DRIVER_GATING])))

    result = _run("--json", "node", "status", "e-1", "--fail-on-blocked")

    assert result.exit_code == 10
    [envelope] = _envelopes(result.stdout)
    assert envelope["error"]["code"] == "node.blocked.nvidia_driver_below_minimum"
    assert envelope["error"]["data"]["phase"] == "idle"   # the verification view, with its reasons
    assert envelope["error"]["data"]["blocking_reasons"] == [DRIVER_GATING]


def test_watch_until_clear_exits_0_once_the_node_is_clear(portal_for, clock):
    blocked, clear = _node(blocking_reasons=[DRIVER_GATING]), _node(blocking_reasons=[])
    portal_for(_SequencePortal([blocked, blocked, clear]))

    result = _run("--json", "node", "status", "e-1", "--watch", "--until-clear", "--timeout", "600")

    assert result.exit_code == 0, result.output
    frames = _envelopes(result.stdout)
    assert [f["ok"] for f in frames] == [True, True, True]
    assert [len(f["data"]["blocking_reasons"]) for f in frames] == [1, 1, 0]
    assert clock.sleeps == [5, 5]


def test_watch_until_clear_exits_10_at_the_timeout(portal_for, clock):
    portal_for(_SequencePortal([_node(blocking_reasons=[DRIVER_GATING])]))

    result = _run("--json", "node", "status", "e-1", "--watch", "--until-clear", "--timeout", "12")

    assert result.exit_code == 10
    frames = _envelopes(result.stdout)
    assert [f["ok"] for f in frames] == [True, True, True, False]
    assert frames[-1]["error"]["code"] == "node.blocked.nvidia_driver_below_minimum"
    assert clock.sleeps == [5, 5, 2]   # the last wait is cut to the deadline


def test_plain_watch_ignores_a_clear_node_and_keeps_refreshing(portal_for, clock):
    portal_for(_SequencePortal([_node(blocking_reasons=[])]))

    def _interrupt(seconds):
        clock.sleeps.append(seconds)
        if len(clock.sleeps) == 3:
            raise KeyboardInterrupt

    clock.sleep = _interrupt

    result = _run("--json", "node", "status", "e-1", "--watch")

    assert result.exit_code == 0, result.output
    assert len(_envelopes(result.stdout)) == 3


def test_until_clear_needs_watch_and_timeout_needs_until_clear(portal_for):
    portal_for(_Portal(node=_node()))

    result = _run("--json", "node", "status", "e-1", "--until-clear")
    assert result.exit_code == 1 and json.loads(result.stdout)["error"]["code"] == "input.arg_invalid"
    result = _run("--json", "node", "status", "e-1", "--watch", "--timeout", "5")
    assert result.exit_code == 1 and "--timeout needs --until-clear" in result.stdout


def test_the_panel_prints_requires_and_the_verify_command(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[DRIVER_GATING])))

    result = _run("node", "get", "e-1")

    lines = [re.sub(r"[│\s]+$", "", re.sub(r"^[│\s]+", "", line)) for line in result.output.splitlines()]
    assert "Requires: sudo · reboot" in lines
    verify = lines.index("Verify:")
    assert lines[verify + 1] == "nvidia-smi --query-gpu=driver_version --format=csv,noheader"
    assert lines.index("sudo apt-get install -y nvidia-driver-580 && sudo reboot") < verify


# The portal's reachability and last-error entries: `gating` is false (it is only about the Secure
# idle-pay gate) and `secure_requirement` follows it, yet the node rents nowhere.
AVAILABILITY_ENTRY = {
    "kind": "availability", "code": "PORT_UNREACHABLE", "gating": False, "secure_requirement": False,
    "title": "The validator could not reach the executor port", "message": "The validator could not reach the executor port",
    "measured": None, "required": None, "fix": "Open port 8080 to the internet and restart the executor.",
    "fix_command": None, "verify_command": None, "requires": [], "docs_url": None,
}
LAST_ERROR_ENTRY = {
    "kind": "last_error", "code": "GPU_VERIFICATION_FAILED", "gating": False, "secure_requirement": False,
    "title": "GPU verification failed", "message": "The GPU proof did not match the reported GPUs",
    "measured": None, "required": None, "fix": "Restart the executor so the validator re-reads its GPUs.",
    "fix_command": None, "verify_command": None, "requires": [], "docs_url": None,
}

# the catalog's current shape: `gating` served next to `secure_requirement`
CATALOG_GPU_MODEL_GATED_FALSE = {**CATALOG_GPU_MODEL, "gating": False, "requires": [], "verify_command": None}


@pytest.mark.parametrize("entry", [AVAILABILITY_ENTRY, LAST_ERROR_ENTRY], ids=["availability", "last_error"])
def test_a_reachability_or_last_error_reason_blocks_whatever_its_gating(portal_for, ansi_console, entry):
    node = _node("e-1", blocking_reasons=[entry])
    portal_for(_Portal(nodes=[node], node=node))

    result = _run("node", "list")

    assert result.exit_code == 0, result.output
    assert "blocked=1" in result.output
    assert re.search(r"1\s+BLOCKED\s+e-1", result.output)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi_console.getvalue())
    assert f"✗ {entry['title']}" in plain
    assert "not eligible for idle pay" not in plain.lower()
    assert "Secure listing" not in plain   # not a Secure requirement, a reason it rents nowhere

    result = _run("--json", "node", "get", "e-1", "--fail-on-blocked")
    assert result.exit_code == 10
    assert json.loads(result.stdout)["error"]["code"] == f"node.blocked.{entry['code']}"


def test_an_idle_pay_reason_with_gating_false_is_not_eligible_and_no_panel(portal_for, ansi_console):
    node = _node("e-1", blocking_reasons=[CATALOG_GPU_MODEL_GATED_FALSE])
    portal_for(_Portal(nodes=[node], node=node))

    result = _run("node", "list")

    assert result.exit_code == 0, result.output
    assert "blocked=0" in result.output and "BLOCKED" not in result.output
    plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi_console.getvalue())
    assert "BLOCKING" not in plain
    assert "◦ e-1: not eligible for idle pay (GPU model outside the idle-pay program); no action needed" in plain
    assert _run("node", "get", "e-1", "--fail-on-blocked").exit_code == 0


def test_plain_watch_with_fail_on_blocked_exits_10_at_the_first_blocked_refresh(portal_for, clock):
    portal_for(_SequencePortal([_node(blocking_reasons=[]), _node(blocking_reasons=[DRIVER_GATING])]))

    result = _run("--json", "node", "status", "e-1", "--watch", "--fail-on-blocked")

    assert result.exit_code == 10
    frames = _envelopes(result.stdout)
    assert [f["ok"] for f in frames] == [True, False]
    assert frames[-1]["error"]["code"] == "node.blocked.nvidia_driver_below_minimum"
    assert clock.sleeps == [5]


# the portal builds a last error's code as `reason_code or title`
LAST_ERROR_WITHOUT_REASON_CODE = {**LAST_ERROR_ENTRY, "code": "GPU verification failed"}


def test_a_last_error_without_reason_code_is_node_blocked_last_error(portal_for):
    portal_for(_Portal(node=_node(blocking_reasons=[LAST_ERROR_WITHOUT_REASON_CODE])))

    result = _run("--json", "node", "get", "e-1", "--fail-on-blocked")

    assert result.exit_code == 10
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "node.blocked.last_error"
    assert "GPU verification failed" in error["message"]

    result = _run("node", "get", "e-1", "--fail-on-blocked")
    assert "[node.blocked.last_error] node e-1 is blocked: GPU verification failed" in result.stderr


def test_a_blocked_code_never_holds_a_space():
    from lium.cli.provider._render import _code_token

    assert _code_token({"code": "PORT_UNREACHABLE", "kind": "availability"}) == "PORT_UNREACHABLE"
    assert _code_token({"code": "GPU verification failed", "kind": "last_error"}) == "last_error"
    assert _code_token({"code": "Port 8080 not reachable!", "kind": "availability"}) == "port_8080_not_reachable"


def test_an_unmapped_error_code_falls_back_to_the_general_namespace():
    from lium.cli.provider._render import error_code_for

    assert error_code_for("SOMETHING_NEW") == "general.something_new"
