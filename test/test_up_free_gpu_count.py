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
