"""The ⚡↓ mark in ``lium ls`` and the three power-limit fields on ``ExecutorInfo``.

The backend judges whether a provider set a GPU's power limit under the card's default
(``gpu_power_limited`` on ``GET /executors``, lium-platform#627) and the portal shows a
"Reduced power limit" badge for it. The CLI shows the same verdict as a mark after the node's
Id, explains it under the table, and carries it in ``--format json``. Held here: only an
explicit ``true`` marks a node; ``false``, ``null`` and a backend without the field show nothing
and never read as "limited".
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display
from lium.cli.ls.display import POWER_LIMITED_MARK, compact_executor, power_limited
from lium.sdk import Config, ExecutorInfo, Lium


def _executor_dict(executor_id: str, **extra) -> dict:
    """A GET /executors?view=summary row, plus whatever the test adds."""
    base = {
        "id": executor_id,
        "machine_name": "NVIDIA GeForce RTX 5090",
        "executor_ip_address": "203.0.113.4",
        "price_per_gpu": 0.5,
        "gpu_count": 1,
        "status": "available",
        "location": {"country": "United States", "country_code": "US"},
        "specs": {
            "gpu": {"count": 1, "details": [{"name": "RTX 5090", "capacity": 32607, "pcie_speed": 32}], "driver": "580.1"},
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760, "free": 8000000},
            "network": {"upload_speed": 480.0, "download_speed": 300.0},
            "sysbox_runtime": False,
        },
        "effective_upload_speed_mbps": 480.0,
        "effective_download_speed_mbps": 300.0,
        "max_cuda_version": 12.8,
        "tier": "secure",
    }
    base.update(extra)
    return base


def _map(executor_dict: dict) -> ExecutorInfo:
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor_dict)


LIMITED = dict(gpu_power_limited=True, gpu_power_limit_w=518, gpu_power_limit_default_w=575)
STOCK = dict(gpu_power_limited=False, gpu_power_limit_w=575, gpu_power_limit_default_w=575)
UNKNOWN = dict(gpu_power_limited=None, gpu_power_limit_w=None, gpu_power_limit_default_w=None)


# -- SDK mapping ---------------------------------------------------------------------------------------


def test_the_three_fields_are_carried_onto_executor_info():
    exe = _map(_executor_dict("limited", **LIMITED))

    assert (exe.gpu_power_limited, exe.gpu_power_limit_w, exe.gpu_power_limit_default_w) == (True, 518, 575)
    assert _map(_executor_dict("stock", **STOCK)).gpu_power_limited is False


def test_null_and_a_backend_without_the_fields_read_as_unknown():
    assert _map(_executor_dict("null", **UNKNOWN)).gpu_power_limited is None
    old = _map(_executor_dict("old"))
    assert (old.gpu_power_limited, old.gpu_power_limit_w, old.gpu_power_limit_default_w) == (None, None, None)


@pytest.mark.parametrize("value", ["true", 1, 0, "yes", [], {}])
def test_anything_but_a_json_boolean_is_unknown_never_limited(value):
    # the summary view is typed, but the CLI must not take a string "true" for a verdict
    assert _map(_executor_dict("odd", gpu_power_limited=value)).gpu_power_limited is None


def test_to_dict_carries_the_fields_for_sdk_callers():
    data = _map(_executor_dict("limited", **LIMITED)).to_dict()

    assert (data["gpu_power_limited"], data["gpu_power_limit_w"], data["gpu_power_limit_default_w"]) == (True, 518, 575)


# -- the table -----------------------------------------------------------------------------------------


def _render(executors: list[ExecutorInfo], width: int = 200) -> str:
    table, *_ = display.build_executors_table(executors, show_pareto=False, width=width)
    console = Console(width=width, record=True, force_terminal=False)
    console.print(table)
    return console.export_text()


def test_only_an_explicit_true_marks_the_id_cell():
    limited = _map(_executor_dict("limited", **LIMITED))
    stock = _map(_executor_dict("stock", **STOCK))
    unknown = _map(_executor_dict("unknown", **UNKNOWN))

    assert power_limited(limited) is True
    assert power_limited(stock) is False
    assert power_limited(unknown) is False

    output = _render([limited, stock, unknown])
    marked = [line for line in output.splitlines() if POWER_LIMITED_MARK in line]
    assert len(marked) == 1
    assert limited.huid in marked[0]
    assert stock.huid not in marked[0] and unknown.huid not in marked[0]


def test_the_mark_follows_the_dind_suffix_and_the_pareto_star():
    exe = _map(_executor_dict("limited", **LIMITED))
    exe.docker_in_docker = True
    table, *_ = display.build_executors_table([exe], show_pareto=True, width=200)
    console = Console(width=200, record=True, force_terminal=False)
    console.print(table)
    [row] = [line for line in console.export_text().splitlines() if exe.huid in line]

    assert f"★ {exe.huid} (DinD) {POWER_LIMITED_MARK}" in row


def test_an_executor_object_without_the_attribute_is_not_marked():
    # other ls tests build SimpleNamespace fakes without the field; a fake is not a limited node
    assert power_limited(SimpleNamespace(huid="fake")) is False


def test_the_legend_explains_the_mark_and_names_the_json_field():
    tip = display.format_tip()

    assert f"{POWER_LIMITED_MARK} = reduced GPU power limit" in tip
    assert "below the card's default" in tip
    assert "gpu_power_limited" in tip


# -- lium ls end to end --------------------------------------------------------------------------------


def _run_ls(monkeypatch, rows: list[dict], *args: str) -> str:
    executors = [_map(row) for row in rows]

    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **kw):
            pass

        def ls(self, **kwargs):
            return executors

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)
    monkeypatch.setattr(ls_command_module.console, "_width", 200)
    monkeypatch.setattr(ls_command_module.console, "_height", 40)
    result = CliRunner().invoke(cli, ["ls", *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_lium_ls_marks_the_limited_node_and_prints_the_legend(monkeypatch):
    output = _run_ls(monkeypatch, [_executor_dict("limited", **LIMITED), _executor_dict("stock", **STOCK)])

    assert output.count(POWER_LIMITED_MARK) == 2  # the one marked row and the legend line
    assert f"{POWER_LIMITED_MARK} = reduced GPU power limit" in output


def test_lium_ls_json_carries_the_verdict_and_the_watts(monkeypatch):
    output = _run_ls(
        monkeypatch,
        [_executor_dict("limited", **LIMITED), _executor_dict("stock", **STOCK), _executor_dict("old")],
        "--format", "json",
    )
    rows = {row["id"]: row for row in json.loads(output)}

    assert (rows["limited"]["gpu_power_limited"], rows["limited"]["gpu_power_limit_w"], rows["limited"]["gpu_power_limit_default_w"]) == (True, 518, 575)
    assert rows["stock"]["gpu_power_limited"] is False
    assert (rows["old"]["gpu_power_limited"], rows["old"]["gpu_power_limit_w"]) == (None, None)


def test_compact_executor_reads_the_fields_off_a_fake_without_them():
    row = compact_executor(SimpleNamespace(
        id="x", huid="fake", gpu_count=1, gpu_type="A100", price_per_gpu=1.0, price_per_hour=1.0, location={},
        specs={}, docker_in_docker=False, max_cuda_version=None, tier=None, link=None, nvlink=None, p2p=None,
        interconnect=None, upload_speed=0.0, download_speed=0.0,
    ), is_pareto=False, index=1)

    assert (row["gpu_power_limited"], row["gpu_power_limit_w"], row["gpu_power_limit_default_w"]) == (None, None, None)


def test_lium_ls_help_explains_the_mark():
    result = CliRunner().invoke(cli, ["ls", "--help"])

    assert result.exit_code == 0
    assert POWER_LIMITED_MARK in result.output
    assert "gpu_power_limited" in result.output
