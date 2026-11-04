"""`lium up -c N` without a NODE_ID matches the free GPUs a rent gets, and `up` keeps `ls` row numbers.

Self-contained on purpose: no import from test_up_auto_select.py, so these cases survive whatever happens to that file.
"""

from types import SimpleNamespace

import pytest

from lium.cli.up.actions import ResolveExecutorAction

# 4-GPU hosts: a rent with no NODE_ID takes the free GPUs, so the half-free one bills 2 to a renter who asked for 4
NODES = [
    SimpleNamespace(huid="half-free", gpu_count=4, available_gpu_count=2),
    SimpleNamespace(huid="all-free", gpu_count=4, available_gpu_count=4),
]


def _resolve(monkeypatch, nodes, **ctx):
    lium = SimpleNamespace(supports=lambda feature: False, ls=lambda **kwargs: list(nodes))
    monkeypatch.setattr("lium.cli.ls.display.sort_executors", lambda executors, show_pareto: (executors, None))
    return ResolveExecutorAction().execute({"lium": lium, **ctx})


@pytest.mark.parametrize(
    ("nodes", "count", "picked"), [(NODES, 4, "all-free"), (NODES, 2, "half-free"), (NODES[:1], 4, None)]
)
def test_count_matches_the_free_gpus_a_rent_gets_not_the_host_total(monkeypatch, nodes, count, picked):
    result = _resolve(monkeypatch, nodes, count=count)

    assert (result.data["executor"].huid if result.ok else None) == picked
    assert result.ok or "GPU count=4" in result.error


def test_auto_select_keeps_the_row_numbers_of_the_last_ls(monkeypatch):
    # `lium up 3` reads the rows `lium ls` stored; picking a node must not overwrite them
    monkeypatch.setattr("lium.cli.ls.command.ls_store_executor", lambda **kwargs: pytest.fail("rewrote the ls rows"))

    assert _resolve(monkeypatch, NODES, count=2).ok


@pytest.mark.parametrize(("free", "shown"), [(2, "2/4×H200"), (4, "4×H200"), (None, "4×H200")])
def test_ls_table_config_names_the_free_gpus_of_a_partly_rented_node(free, shown):
    # a renter read "8×H200" on a node with 2 free and its `up <node> -c 8` was refused (3 Nov)
    from lium.cli.ls.display import _cfg, _table_cfg

    node = SimpleNamespace(gpu_type="H200", gpu_count=4, available_gpu_count=free)
    assert (_table_cfg(node), _cfg(node)) == (shown, "4×H200")  # JSON `config` keeps the host total


def test_ls_table_config_keeps_a_long_gpu_name_whole_on_a_wide_terminal():
    # live 4 Nov: "1/8×RTXPRO6000" was cropped to "1/8×RTXPRO6…" at 220 columns
    from rich.console import Console

    from lium.cli.ls.display import build_executors_table
    from lium.sdk import ExecutorInfo

    node = ExecutorInfo(
        id="e1", huid="zesty-wolf-43", machine_name="NVIDIA RTX PRO 6000D", gpu_type="RTXPRO6000D", gpu_count=8,
        price_per_hour=14.0, price_per_gpu=1.75, location={"country": "Kazakhstan"}, specs={}, status="available",
        docker_in_docker=False, ip="10.0.0.1", available_gpu_count=1,
    )
    table, *_ = build_executors_table([node], width=220)
    console = Console(width=220, record=True)
    console.print(table)
    assert "1/8×RTXPRO6000D" in console.export_text()
