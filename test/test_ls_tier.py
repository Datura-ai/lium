"""Tests for spot/secure tier surfacing in ``lium ls`` (SDK mapping + display)."""

from __future__ import annotations

from lium.sdk import Config, Lium
from lium.cli.ls import display


def _make_executor_dict(executor_id: str, tier) -> dict:
    """Minimal executor dict as returned by the /executors endpoint."""
    return {
        "id": executor_id,
        "machine_name": "NVIDIA H100 SXM 8x",
        "executor_ip_address": "1.2.3.4",
        "price_per_gpu": 2.5,
        "status": "available",
        "location": {"country": "US"},
        "specs": {
            "gpu": {
                "count": 8,
                "details": [{"name": "H100", "capacity": 81920, "pcie_speed": 16}],
                "driver": "535.104.05",
            },
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "sysbox_runtime": False,
        },
        "tier": tier,
    }


def _map(executor_dict: dict):
    """Run the executor dict through the SDK mapping layer."""
    client = Lium(Config(api_key="test-key"))
    return client._dict_to_executor_info(executor_dict)


def test_dict_to_executor_info_maps_tier():
    """Tier from the API payload is carried onto ExecutorInfo."""
    assert _map(_make_executor_dict("e1", "spot")).tier == "spot"
    assert _map(_make_executor_dict("e2", "secure")).tier == "secure"


def test_dict_to_executor_info_tier_absent_is_none():
    """Missing tier key maps to None (no crash, no assumed value)."""
    d = _make_executor_dict("e3", None)
    del d["tier"]
    assert _map(d).tier is None


def test_tier_display_renders_spot_and_secure():
    """_tier_display surfaces the tier word; unknown renders as a dash."""
    assert "spot" in display._tier_display(_map(_make_executor_dict("e1", "spot")))
    assert "secure" in display._tier_display(_map(_make_executor_dict("e2", "secure")))
    assert display._tier_display(_map(_make_executor_dict("e3", None))) == "—"


def test_table_has_tier_column():
    """The ls table exposes a Tier column."""
    executors = [_map(_make_executor_dict("e1", "spot"))]
    table, *_ = display.build_executors_table(executors, show_pareto=False)
    headers = [c.header for c in table.columns]
    assert "Tier" in headers


def test_compact_executor_includes_tier():
    """JSON view (lium ls --output json) exposes tier for agents."""
    exe = _map(_make_executor_dict("e1", "spot"))
    assert display.compact_executor(exe, is_pareto=False, index=1)["tier"] == "spot"
