"""Portal text is shown as text, never parsed as Rich markup.

Every cell of the provider tables is a Rich markup string (``console.get_styled``
wraps values in style tags), and the portal hands back strings other providers
typed or their executors reported. ``_gpu_config``, ``_ip_port``,
``_value_or_dash`` and the record renderer used to interpolate them raw: a GPU
name ``[/]`` raised ``MarkupError`` inside ``console.print(table)`` and
``node list --all`` exited without a table; ``H100[red]`` was shown as ``H100``
(ticket-0260 report 7). Only ``computed_status`` was escaped before.
"""

from __future__ import annotations

import pytest

from lium.cli.provider import _render


@pytest.fixture(autouse=True)
def wide_console(monkeypatch):
    """Enough columns that no cell is cut to an ellipsis, so the assertions read whole values."""
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setenv("TERM", "xterm-256color")  # Rich ignores COLUMNS under TERM=dumb and sizes to 80


def _node(**overrides):
    row = {
        "id": "node-0123456789",
        "executor_ip_address": "10.0.0.1",
        "executor_ip_port": 22,
        "price_per_gpu": 1.5,
        "gpu_count": 8,
        "gpu_type": "H100",
        "rented_gpu_count": 0,
        "revenue_per_hour": 0,
        "computed_status": {"status": "AVAILABLE"},
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize("gpu_type", ["[/]", "[/var/log/x]", "[bold red]"])
def test_a_bracketed_gpu_name_does_not_crash_the_node_table(capsys, gpu_type):
    _render._render_rows([_node(gpu_type=gpu_type)])  # raised MarkupError before
    assert f"8×{gpu_type}" in capsys.readouterr().out


def test_markup_in_a_gpu_name_is_shown_not_applied(capsys):
    _render._render_rows([_node(gpu_type="H100[red]"), _node(gpu_type="[bold]FAKE[/bold]")])
    out = capsys.readouterr().out
    assert "8×H100[red]" in out
    assert "8×[bold]FAKE[/bold]" in out


def test_endpoint_and_id_cells_are_escaped(capsys):
    _render._render_rows([_node(id="[/]", executor_ip_address="[link=http://x]10.0.0.1[/link]")])
    out = capsys.readouterr().out
    assert "[/]" in out and "[link=http://x]10.0.0.1[/link]:22" in out


def test_machine_request_cells_are_escaped(capsys):
    row = {"id": "r-1", "machine_name": "[/]", "gpu_count": 8, "cpu": "[bold]x[/bold]", "ram": "64G", "achieved": False}
    _render._render_rows([row])
    out = capsys.readouterr().out
    assert "[/]" in out and "[bold]x[/bold]" in out


def test_the_generic_table_and_its_headers_are_escaped(capsys):
    _render._render_generic_rows([{"[/]": "[/]", "note": "see [bold]this[/bold]", "tags": ["[/]"]}])
    out = capsys.readouterr().out
    assert "see [bold]this[/bold]" in out and "[/]" in out


def test_a_single_record_shows_bracketed_values_as_text(capsys):
    _render._render_record(
        {
            "name": "[bold]node[/bold]",
            "id": "[/]",
            "labels": ["[/x]", "b"],
            "specs": {"gpu": "[red]H100[/red]"},
            "created_at": "2026-09-16T10:20:10Z",
        }
    )
    out = capsys.readouterr().out
    assert "[bold]node[/bold]" in out
    assert "[/x], b" in out
    assert "gpu=[red]H100[/red]" in out


def test_computed_status_stays_escaped(capsys):
    """The one place that was escaped before must still be."""
    _render._render_record({"computed_status": {"status": "[/]", "message": "see [/var/log/x]"}})
    assert "[/] — see [/var/log/x]" in capsys.readouterr().out
