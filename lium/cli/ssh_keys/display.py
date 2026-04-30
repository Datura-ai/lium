"""SSH-keys display formatting."""

from typing import List, Tuple

from rich.table import Table
from rich.text import Text

from lium.sdk import SSHKey
from lium.cli import ui
from lium.cli.utils import format_date, mid_ellipsize


def _public_key_short(public_key: str) -> str:
    parts = (public_key or "").strip().split()
    if len(parts) < 2:
        return mid_ellipsize(public_key or "—", width=24)
    body = parts[1]
    if len(body) <= 24:
        body_short = body
    else:
        body_short = f"{body[:10]}…{body[-10:]}"
    return f"{parts[0]} {body_short}"


def build_ssh_keys_table(keys: List[SSHKey]) -> Tuple[Table, str]:
    header = f"{Text('SSH keys', style='bold')}  ({len(keys)} total)"

    table = Table(
        show_header=True,
        header_style="dim",
        box=None,
        pad_edge=False,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("", justify="right", width=3, no_wrap=True, style="dim")
    table.add_column("ID", justify="left", ratio=2, min_width=20, overflow="fold")
    table.add_column("Name", justify="left", ratio=2, min_width=15, overflow="ellipsis")
    table.add_column("Public key", justify="left", ratio=4, min_width=25, overflow="ellipsis")
    table.add_column("Created", justify="right", width=12, no_wrap=True)

    for idx, key in enumerate(keys, 1):
        table.add_row(
            str(idx),
            ui.styled(mid_ellipsize(key.id) if key.id else "—", "id"),
            ui.styled(key.name or "—", "info"),
            _public_key_short(key.public_key),
            format_date(key.created_at) if key.created_at else "—",
        )

    return table, header
