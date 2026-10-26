"""`lium ps` stays readable at 80 columns and behind a pipe: one line per huid, ports collapsed (DAH-5148)."""

import io

import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from rich.console import Console

from lium.cli.ps import display
from lium.sdk import PodInfo


def _pod(huid: str, price: float = 0.24, gpu: str = "A4000", gpus: int = 1, created_at: str = "2026-10-25T00:00:00Z",
         removal: str | None = None) -> PodInfo:
    ports = {"22": 20301, **{str(8000 + i): 20302 + i for i in range(10)}}
    executor = SimpleNamespace(ip="203.0.113.7", price_per_hour=price, gpu_type=gpu, gpu_count=gpus, specs={}, id="e")
    return PodInfo(
        id=f"id-{huid}", name="train", huid=huid, status="RUNNING", ssh_cmd="ssh root@203.0.113.7 -p 20301",
        ports=ports, created_at=created_at, updated_at="", executor=executor,
        template={"name": "pytorch"}, removal_scheduled_at=removal, jupyter_installation_status=None, jupyter_url=None,
    )


def test_ports_collapse_to_the_ssh_mapping_and_a_count():
    assert display._format_ports({"8000": 20302, "22": 20301, "8001": 20303}) == "22:20301 +2"
    assert display._format_ports({"22": 20301}) == "22:20301"
    assert display._format_ports({}) == "—"


def test_ps_table_at_80_columns_keeps_each_huid_on_one_line():
    pods = [_pod("noble-matrix-27"), _pod("eager-wolf-aa")]
    for short in (False, True):  # piped (Ports shown) and a narrow terminal (Ports hidden)
        table, _ = display.build_pods_table(pods, short=short)
        out = io.StringIO()
        Console(file=out, width=80, force_terminal=False).print(table)
        text = out.getvalue()
        assert "noble-matrix-27" in text and "eager-wolf-aa" in text
        assert max(len(line) for line in text.splitlines()) <= 80
        assert ("22:20301 +10" in text) is not short


@pytest.mark.parametrize("status", ["RUNNING", "REBOOT_FAILED"])
@pytest.mark.parametrize("capped", [False, True])
@pytest.mark.parametrize("short, width", [(True, 80), (False, 120)])  # narrow terminal; a pipe or --wide
def test_ps_table_keeps_huids_and_amounts_whole(status, capped, short, width):
    # 8x RTXPRO6000 at $8/h, 25 hours old, removal at 60 hours: "$200.00/$480.00"
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=25)
    big = _pod("noble-matrix-27", price=8.0, gpu="RTXPRO6000", gpus=8, created_at=created.isoformat(),
               removal=(created + timedelta(hours=60)).isoformat() if capped else None)
    big.status = status
    table, _ = display.build_pods_table([big, _pod("eager-wolf-aa")], short=short)
    out = io.StringIO()
    Console(file=out, width=width, force_terminal=False).print(table)
    text = out.getvalue()
    assert max(len(line) for line in text.splitlines()) <= width
    assert "Name" in text and ("22:20301 +10" in text) is not short  # no column is cropped away
    for whole in ("noble-matrix-27", "eager-wolf-aa", "$8.00", "$0.24", "$200.00/$480.00" if capped else "$200.00"):
        assert whole in text, f"{whole!r} cut:\n{text}"


@pytest.mark.parametrize("term", ["xterm-256color", "dumb"])
@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("wide", [False, True])
def test_ps_render_at_80_columns_never_crops(monkeypatch, terminal, wide, term):
    monkeypatch.setenv("TERM", term)  # Rich ignores a lone width override on a dumb terminal
    # print(width=) is clamped to the console's own width, so the wide layout widens the console itself
    from lium.cli import ui
    from lium.cli.ps import command
    from lium.cli.themed_console import ThemedConsole

    out = io.StringIO()
    piped = ThemedConsole(file=out, force_terminal=terminal, width=80)
    monkeypatch.setattr(command, "console", piped)
    monkeypatch.setattr(ui, "console", piped)
    created = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=25)
    big = _pod("noble-matrix-27", price=8.0, gpu="RTXPRO6000", gpus=8, created_at=created.isoformat(),
               removal=(created + timedelta(hours=60)).isoformat())
    big.status = "REBOOT_FAILED"

    command._render([big, _pod("eager-wolf-aa")], "table", wide=wide, filtered=False, show_index=True)

    from rich.text import Text

    text = Text.from_ansi(out.getvalue()).plain
    narrow = terminal and not wide  # Ports hidden, Status may wrap
    for whole in ("noble-matrix-27", "eager-wolf-aa", "$8.00", "$200.00/$480.00", "Name"):
        assert whole in text, f"{whole!r} cut:\n{text}"
    if not narrow:
        for whole in ("REBOOT_FAILED", "203.0.113.7", "22:20301 +10"):
            assert whole in text, f"{whole!r} cut:\n{text}"


def test_ps_watch_follows_a_resize_after_the_wide_layout(monkeypatch):
    # the widening is undone after each render, so a 140 -> 80 resize brings back the narrow layout
    from lium.cli import ui
    from lium.cli.ps import command
    from lium.cli.themed_console import ThemedConsole

    out = io.StringIO()
    tty = ThemedConsole(file=out, force_terminal=True)
    monkeypatch.setattr(command, "console", tty)
    monkeypatch.setattr(ui, "console", tty)
    pods = [_pod("noble-matrix-27"), _pod("eager-wolf-aa")]

    monkeypatch.setenv("COLUMNS", "140")
    command._render(pods, "table", wide=False, filtered=False, show_index=True)
    assert "22:20301 +10" in out.getvalue()

    out.seek(0), out.truncate()
    monkeypatch.setenv("COLUMNS", "80")
    command._render(pods, "table", wide=False, filtered=False, show_index=True)
    assert "22:20301 +10" not in out.getvalue() and tty.width == 80
