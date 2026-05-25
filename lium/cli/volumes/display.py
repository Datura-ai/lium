"""Volumes display formatting."""

from typing import List
from rich.table import Table
from rich.text import Text

from lium.sdk import VolumeInfo
from lium.cli import ui
from lium.cli.utils import mid_ellipsize, format_date


def format_size(size_gb: float) -> str:
    if size_gb is None or size_gb == 0:
        return "0"
    elif size_gb < 0.01:
        return f"{size_gb * 1024:.1f} MB"
    elif size_gb < 1:
        return f"{size_gb:.2f} GB"
    else:
        return f"{size_gb:.1f} GB"


def format_file_count(count: int) -> str:
    if count is None or count == 0:
        return "0"
    elif count < 1000:
        return str(count)
    elif count < 1000000:
        return f"{count / 1000:.1f}K"
    else:
        return f"{count / 1000000:.1f}M"


def build_volumes_table(volumes: List[VolumeInfo]) -> tuple[Table, str, str]:
    header = f"{Text('Volumes', style='bold')}  ({len(volumes)} total)"
    tip = f"Tip: {ui.styled('lium up <node> --volume id:<HUID>', 'success')} {ui.styled('# attach volume to pod', 'dim')}"

    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )

    table.add_column("", justify="right", width=3, no_wrap=True, style="dim")
    table.add_column("ID", justify="left", ratio=3, min_width=20, overflow="fold")
    table.add_column("Name", justify="left", ratio=3, min_width=15, overflow="ellipsis")
    table.add_column("Size", justify="right", width=10, no_wrap=True)
    table.add_column("Files", justify="right", width=8, no_wrap=True)
    table.add_column("Description", justify="left", ratio=4, min_width=20, overflow="ellipsis")
    table.add_column("Created", justify="right", width=12, no_wrap=True)

    for idx, volume in enumerate(volumes, 1):
        table.add_row(
            str(idx),
            ui.styled(mid_ellipsize(volume.huid), 'id'),
            ui.styled(volume.name or "—", 'info'),
            format_size(volume.current_size_gb),
            format_file_count(volume.current_file_count),
            ui.styled(volume.description or "—", 'dim'),
            format_date(volume.created_at),
        )

    return table, header, tip
