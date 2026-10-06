"""DAH-3079: `lium ls` lists the cheapest $/GPU·h first (the ★ marks value, it no longer ranks) and
`up <index>` is numbered from that same order.

Owner, 7 Sep 2026: "make defaults sane (sorted by $/GPU …)". Until now the default put the fastest network
first at any price; B-62 (lium#164) is a renter who paid $0.58/h with $0.30/h nodes lower in the same list.
"""

from types import SimpleNamespace

from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display


def _executor(huid: str, price_per_gpu: float, download: int, vram: int = 80) -> SimpleNamespace:
    # the metric set calculate_pareto_frontier reads; `vram` lets a test decide who is ★
    return SimpleNamespace(
        id=huid,
        huid=huid,
        machine_name="NVIDIA H100",
        gpu_type="H100",
        gpu_count=1,
        price_per_gpu=price_per_gpu,
        price_per_hour=price_per_gpu,
        location={"country": "United States", "country_code": "US"},
        specs={
            "gpu": {"details": [{"capacity": vram * 1024, "pcie_speed": 16, "memory_speed": 2000, "graphics_speed": 1500}]},
            "ram": {"total": 512 * 1024 * 1024},
            "hard_disk": {"total": 2000 * 1024 * 1024},
            "network": {"download_speed": download, "upload_speed": download},
        },
        docker_in_docker=True,
        max_cuda_version=12.8,
        tier="secure",
        upload_speed=download,
        download_speed=download,
        effective_download_speed_mbps=download,
        effective_upload_speed_mbps=download,
    )


FLEET = [
    _executor("fast-pricey", price_per_gpu=2.40, download=9000),
    _executor("cheap-slow", price_per_gpu=0.30, download=100),
    _executor("mid", price_per_gpu=1.00, download=3000),
    _executor("dominated", price_per_gpu=2.60, download=50, vram=24),
]


def test_default_order_is_cheapest_per_gpu_first_and_the_star_marks_not_ranks():
    ordered, stars = display.sort_executors(FLEET)

    assert [e.huid for e in ordered] == ["cheap-slow", "mid", "fast-pricey", "dominated"]
    assert dict(zip((e.huid for e in ordered), stars))["fast-pricey"] is True
    assert dict(zip((e.huid for e in ordered), stars))["dominated"] is False


def test_a_node_without_a_price_sorts_last_not_first():
    """The SDK stores a missing price as 0; it must not become row 1 and the node `lium up 1` rents."""
    unpriced = _executor("no-price", price_per_gpu=0, download=5000)

    ordered, _ = display.sort_executors([unpriced] + FLEET)

    assert ordered[0].huid == "cheap-slow"
    assert ordered[-1].huid == "no-price"

    by_total, _ = display.sort_executors([unpriced] + FLEET, sort_by="price_total")
    assert by_total[-1].huid == "no-price"


def test_explicit_sort_still_applies():
    ordered, _ = display.sort_executors(FLEET, sort_by="download")

    assert ordered[0].huid == "fast-pricey"


def test_up_index_is_numbered_from_the_order_ls_prints(monkeypatch):
    stored = []
    monkeypatch.setattr(ls_command_module, "Lium", lambda *a, **k: SimpleNamespace(ls=lambda **kw: list(FLEET)))
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: stored.append([e.huid for e in executors]))

    returned = ls_command_module.ls_store_executor(gpu_type="H100")

    assert stored == [[e.huid for e in display.sort_executors(FLEET)[0]]]
    assert [e.huid for e in returned] == stored[0]
