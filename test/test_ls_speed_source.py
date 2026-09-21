"""``lium ls`` tells a measured speed from the node's own report.

The Download / Upload columns show the backend's effective figure — the first one it trusts out of a
VerifyX average, a speed-test average and the node's own scrape (``specs.network.*_speed``). Two nodes
listing "300" could be one the validator measured at 300 Mbps and one that merely says so. ``~300`` now
marks the second kind, the tip explains the mark, ``--format json`` carries it as ``download_source`` /
``upload_source``, and ``--sort download`` orders by the figure the column shows (it read the raw scrape).
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import display
from lium.sdk import Config, ExecutorInfo, Lium


def _executor_dict(executor_id: str, *, raw_download=300.0, raw_upload=480.0, effective_download=300.0, effective_upload=480.0) -> dict:
    """One `GET /executors?view=summary` row: the effective figures and the raw scrape, nothing in between."""
    network = {}
    if raw_download is not None:
        network["download_speed"] = raw_download
    if raw_upload is not None:
        network["upload_speed"] = raw_upload
    row = {
        "id": executor_id,
        "machine_name": "NVIDIA H200 8x",
        "executor_ip_address": "203.0.113.4",
        "price_per_gpu": 3.25,
        "status": "available",
        "location": {"country": "United States", "country_code": "US"},
        "specs": {
            "gpu": {"count": 8, "details": [{"name": "H200", "capacity": 143771, "pcie_speed": 32}], "driver": "570.86"},
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "network": network,
            "sysbox_runtime": False,
        },
        "max_cuda_version": 12.8,
        "tier": "secure",
    }
    if effective_download is not None:
        row["effective_download_speed_mbps"] = effective_download
    if effective_upload is not None:
        row["effective_upload_speed_mbps"] = effective_upload
    return row


def _map(executor_dict: dict) -> ExecutorInfo:
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor_dict)


def _table_text(executors: list[ExecutorInfo]) -> str:
    table, *_ = display.build_executors_table(executors, show_pareto=False)
    from rich.console import Console

    console = Console(width=400, record=True, color_system=None)
    console.print(table)
    return console.export_text()


# -- the source of a figure ------------------------------------------------------------------------------


def test_an_effective_figure_that_differs_from_the_scrape_is_measured():
    exe = _map(_executor_dict("hgx", raw_download=300.0, effective_download=2400.1))

    assert display.network_speed(exe, "download") == (2400.1, display.MEASURED)


def test_an_effective_figure_equal_to_the_scrape_is_the_node_s_own_report():
    # the backend's chain fell through every measurement to `specs.network.download_speed`
    exe = _map(_executor_dict("plain", raw_download=300.0, effective_download=300.0))

    assert display.network_speed(exe, "download") == (300.0, display.REPORTED)


def test_a_measured_figure_needs_no_scrape_to_compare_against():
    exe = _map(_executor_dict("verifyx-only", raw_download=None, effective_download=1500.0))

    assert display.network_speed(exe, "download") == (1500.0, display.MEASURED)


def test_no_effective_figure_falls_back_to_the_scrape_and_says_so():
    # an API that predates `effective_*_speed_mbps`: the raw figure is shown, marked as the node's own
    exe = _map(_executor_dict("old-api", raw_download=300.0, effective_download=None))

    assert display.network_speed(exe, "download") == (300.0, display.REPORTED)


def test_no_figure_at_all_is_unknown():
    exe = _map(_executor_dict("silent", raw_download=None, raw_upload=None, effective_download=None, effective_upload=None))

    assert display.network_speed(exe, "download") == (None, None)
    assert display.network_speed(exe, "upload") == (None, None)


def test_a_zero_or_non_numeric_figure_counts_as_missing():
    exe = _map(_executor_dict("zeroed", raw_download=0, effective_download=0))
    assert display.network_speed(exe, "download") == (None, None)

    junk = _executor_dict("junk", raw_download="fast", effective_download=None)
    assert display.network_speed(_map(junk), "download") == (None, None)


def test_upload_reads_the_same_way():
    exe = _map(_executor_dict("hgx", raw_upload=480.0, effective_upload=1200.5))
    assert display.network_speed(exe, "upload") == (1200.5, display.MEASURED)

    exe = _map(_executor_dict("plain", raw_upload=480.0, effective_upload=480.0))
    assert display.network_speed(exe, "upload") == (480.0, display.REPORTED)


# -- the table -------------------------------------------------------------------------------------------


def test_the_table_prefixes_the_node_s_own_figure_and_not_a_measured_one():
    measured = _map(_executor_dict("measured", raw_download=300.0, effective_download=2400.1, raw_upload=480.0, effective_upload=1200.5))
    reported = _map(_executor_dict("reported", raw_download=300.0, effective_download=300.0, raw_upload=480.0, effective_upload=480.0))

    assert display._specs_row(measured)["Download"] == "2400"
    assert display._specs_row(measured)["Upload"] == "1200"
    assert display._specs_row(reported)["Download"] == "~300"
    assert display._specs_row(reported)["Upload"] == "~480"

    text = _table_text([measured, reported])
    assert "2400" in text and "~300" in text and "~2400" not in text


def test_a_node_with_no_figure_still_shows_a_dash():
    silent = _map(_executor_dict("silent", raw_download=None, raw_upload=None, effective_download=None, effective_upload=None))

    assert display._specs_row(silent)["Download"] == "—"
    assert display._specs_row(silent)["Upload"] == "—"


def test_a_slow_reported_node_keeps_the_warning_colour(monkeypatch):
    styled = []
    monkeypatch.setattr(display.console, "get_styled", lambda text, style: styled.append((text, style)) or text)
    slow = _map(_executor_dict("slow", raw_download=80.0, effective_download=80.0))

    display.build_executors_table([slow], show_pareto=False)

    assert ("~80", "warning") in styled


def test_the_tip_explains_the_mark():
    assert display.REPORTED_FOOTNOTE in display.format_tip()
    assert "node's own figure" in display.REPORTED_FOOTNOTE


def test_the_help_text_explains_the_mark():
    result = CliRunner().invoke(cli, ["ls", "--help"])

    assert result.exit_code == 0, result.output
    assert '"~"' in result.output
    assert "node's own report" in result.output


# -- json ------------------------------------------------------------------------------------------------


def test_json_rows_carry_the_source_next_to_the_figure():
    measured = display.compact_executor(_map(_executor_dict("measured", raw_download=300.0, effective_download=2400.1)), is_pareto=False, index=1)
    reported = display.compact_executor(_map(_executor_dict("reported", raw_download=300.0, effective_download=300.0)), is_pareto=False, index=2)
    silent = display.compact_executor(
        _map(_executor_dict("silent", raw_download=None, raw_upload=None, effective_download=None, effective_upload=None)), is_pareto=False, index=3
    )

    assert (measured["download_mbps"], measured["download_source"]) == (2400, "measured")
    assert (reported["download_mbps"], reported["download_source"]) == (300, "reported")
    assert (silent["download_mbps"], silent["download_source"]) == (None, None)
    assert (silent["upload_mbps"], silent["upload_source"]) == (None, None)
    # the number stays a number: the `~` is the table's, never the JSON's
    assert isinstance(reported["download_mbps"], int)


# -- sort ------------------------------------------------------------------------------------------------


def test_sort_download_orders_by_the_figure_the_column_shows():
    # the scrape says A is faster (900 vs 100); the validator measured B faster (500 vs 200)
    a = _map(_executor_dict("a", raw_download=900.0, effective_download=200.0))
    b = _map(_executor_dict("b", raw_download=100.0, effective_download=500.0))
    silent = _map(_executor_dict("silent", raw_download=None, effective_download=None))

    ordered, _ = display.sort_executors([a, silent, b], sort_by="download", show_pareto=False)

    assert [e.id for e in ordered] == ["b", "a", "silent"]


def test_sort_upload_orders_by_the_figure_the_column_shows():
    a = _map(_executor_dict("a", raw_upload=900.0, effective_upload=200.0))
    b = _map(_executor_dict("b", raw_upload=100.0, effective_upload=500.0))

    ordered, _ = display.sort_executors([a, b], sort_by="upload", show_pareto=False)

    assert [e.id for e in ordered] == ["b", "a"]


def test_ls_json_end_to_end_carries_the_source(monkeypatch):
    from types import SimpleNamespace

    from lium.cli.ls import command as ls_command_module

    fleet = [
        _map(_executor_dict("measured", raw_download=300.0, effective_download=2400.1)),
        _map(_executor_dict("reported", raw_download=300.0, effective_download=300.0)),
    ]

    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)

        def __init__(self, *a, **k):
            pass

        def ls(self, **kwargs):
            return fleet

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)

    result = CliRunner().invoke(cli, ["ls", "--format", "json"])

    assert result.exit_code == 0, result.output
    rows = {row["id"]: row for row in json.loads(result.output)}
    assert rows["measured"]["download_source"] == "measured"
    assert rows["reported"]["download_source"] == "reported"
