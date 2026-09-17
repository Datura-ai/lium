"""DAH-3043: ``lium ls`` has to be usable at the width a caller actually has.

At the default 80 columns (every non-TTY caller, most fresh terminals) Rich
squeezed all thirteen columns evenly, so the table lost the Id and the index and
printed prices as ``0…`` — the three things a renter needs to run ``lium up``.
The table now keeps a fixed core set at any width and drops the rest by priority.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command_module
from lium.cli.ls import display

CORE = ["", "Id", "Config", "$/GPU·h", "Location"]
ALL = [header for header, *_ in display._COLUMNS]


def _executor(huid: str, price: float, country: str = "United States") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"id-{huid}",
        huid=huid,
        gpu_type="A100",
        gpu_count=8,
        price_per_hour=price * 8,
        price_per_gpu=price,
        location={"country": country},
        download_speed=1224,
        upload_speed=542,
        specs={
            "gpu": {"count": 8, "details": [{"name": "A100", "capacity": 81920, "pcie_speed": 16}]},
            "ram": {"total": 1857028096},
            "hard_disk": {"total": 20323254272},
            "network": {"download_speed": 1224, "upload_speed": 542},
            "available_port_count": 298,
            "location": {"country": country},
        },
        docker_in_docker=True,
        max_cuda_version=13.0,
        tier="spot",
        interconnect=None,
        nvlink=None,
        link=None,
        p2p=None,
    )


def _run_ls(monkeypatch, columns: int) -> str:
    """``lium ls`` as a caller with a ``columns``-wide terminal sees it.

    The size is pinned on the console directly: Rich reads ``COLUMNS`` from the
    environment, but ``FORCE_COLOR`` plus ``TERM=dumb`` in a developer's shell
    would make the same test render at 80 no matter what the test exports.
    """
    executors = [_executor("cosmic-hawk-2e", 0.40), _executor("golden-matrix-ff", 12.50, "United Arab Emirates")]

    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)   # a server without workspaces: no context line

        def __init__(self, *args, **kwargs):
            pass

        def ls(self, **kwargs):
            return executors

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)
    monkeypatch.setattr(ls_command_module.console, "_width", columns)
    monkeypatch.setattr(ls_command_module.console, "_height", 25)

    result = CliRunner().invoke(cli, ["ls"])
    assert result.exit_code == 0, result.output
    return result.output


def _header_line(output: str) -> list:
    """Column names of the rendered header row."""
    line = next(line for line in output.splitlines() if " Id " in line)
    return [name.strip() for name in line.split("  ") if name.strip()]


def test_80_columns_keeps_index_id_config_price_and_country(monkeypatch):
    output = _run_ls(monkeypatch, 80)
    header = _header_line(output)

    assert header == ["Id", "Config", "Tier", "$/GPU·h", "Location"]
    # The rows a renter acts on: index, full Id, untruncated price, country.
    assert "1  ★ cosmic-hawk-2e (DinD)" in output
    assert "0.40" in output and "12.50" in output
    assert "0…" not in output and "1…" not in output
    assert "United States" in output
    assert "9 more columns hidden — widen the terminal or use --format json" in output   # CPUs (DAH-2981) and Link (DAH-2924) joined the table
    assert all(len(line) <= 80 for line in output.splitlines())


def test_120_columns_adds_download_and_vram_before_the_rest(monkeypatch):
    output = _run_ls(monkeypatch, 120)
    header = _header_line(output)

    assert header == ["Id", "Config", "Tier", "$/GPU·h", "Location", "VRAM (Gb)", "Download (Mbps)"]
    assert "1224" in output and "80" in output
    assert "7 more columns hidden" in output   # CPUs (DAH-2981) and Link (DAH-2924) joined the table
    assert all(len(line) <= 120 for line in output.splitlines())


def test_200_columns_shows_every_column_and_no_footer(monkeypatch):
    output = _run_ls(monkeypatch, 200)
    header = _header_line(output)

    assert header == ALL[1:]
    assert "hidden" not in output


@pytest.mark.parametrize("width", [40, 60, 79, 80, 81, 100, 120, 160, 170, 300])
def test_fit_columns_keeps_the_core_set_and_fits_the_width(width):
    shown, hidden = display.fit_columns(width)

    assert [h for h in shown if h in CORE] == CORE
    assert sorted(shown + hidden) == sorted(ALL)
    assert shown == [h for h in ALL if h in shown], "display order is preserved"
    nominal = {header: nominal for header, nominal, *_ in display._COLUMNS}
    core_width = sum(nominal[h] for h in CORE) + display._COLUMN_GAP * (len(CORE) - 1)
    if width >= core_width:
        assert sum(nominal[h] for h in shown) + display._COLUMN_GAP * (len(shown) - 1) <= width


def test_fit_columns_drops_in_priority_order():
    """A wider terminal never loses a column a narrower one had, and the optional columns
    present are always the first N of the priority list (Tier, Download, VRAM, Link, Max CUDA,
    Upload, RAM, CPUs, Disk free, Ports) — a swap of two priorities fails here."""
    by_priority = [h for h, *_ in sorted((c for c in display._COLUMNS if c[2] is not None), key=lambda c: c[2])]
    assert by_priority == ["Tier", "Download (Mbps)", "VRAM (Gb)", "Link", "Max CUDA", "Upload (Mbps)", "RAM (Gb)", "CPUs", "Disk free (Gb)", "Ports"]
    previous: set = set()
    for width in range(60, 220):
        shown, hidden = display.fit_columns(width)
        assert previous <= set(shown)
        previous = set(shown)
        optional = [h for h in shown if h not in CORE]
        assert sorted(optional, key=by_priority.index) == by_priority[: len(optional)], width
        assert sorted(hidden, key=by_priority.index) == by_priority[len(optional):], width
    assert display.fit_columns(None) == (ALL, [])


def test_no_row_folds_at_any_width_above_the_core_minimum():
    """arhangel66's thread on 18a265d: the nominal widths and Rich's ratios disagreed, so at widths where
    fit_columns admitted a column with no slack Rich squeezed Location and `United States` folded onto a
    second line. Rendered through Rich at every width from the 69-column core minimum to 220, two rows
    stay two lines."""
    from rich.console import Console

    executors = [_executor("cosmic-hawk-2e", 0.40), _executor("golden-matrix-ff", 12.50)]
    for width in range(69, 221):
        table, *_ = display.build_executors_table(executors, show_pareto=False, width=width)
        console = Console(width=width, record=True, force_terminal=False)
        console.print(table)
        lines = [line for line in console.export_text().splitlines() if line.strip()]
        assert len(lines) == 1 + len(executors), f"width {width}: {lines}"


def test_build_executors_table_without_width_keeps_every_column():
    """Callers that render for themselves (and the other ls tests) are unchanged."""
    table, *_ = display.build_executors_table([_executor("cosmic-hawk-2e", 0.40)], show_pareto=False)
    assert [c.header for c in table.columns] == ALL
