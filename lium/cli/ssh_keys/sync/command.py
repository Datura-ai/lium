"""`lium ssh-keys sync` command — backfill registration for legacy pods."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import ensure_config, handle_errors

from ..display import build_ssh_keys_table
from .actions import SyncSSHKeysAction


@click.command("sync")
@handle_errors
def ssh_keys_sync_command():
    """Register every local SSH pubkey that isn't already on Lium.

    Reads ``~/.ssh/*.pub`` (via the SDK's discovery), registers anything missing
    from ``GET /ssh-keys`` under ``cli-<user>@<hostname>``, and surfaces any
    legacy ``sdk-<pod_id>`` keys (auto-created by the backend before this CLI
    started owning registration) that you may want to delete from the dashboard.
    """
    ensure_config()

    lium = Lium(source="cli")
    ctx = {"lium": lium}

    action = SyncSSHKeysAction()
    result = ui.load("Syncing SSH keys", lambda: action.execute(ctx))

    if not result.ok:
        ui.error(result.error)
        return

    registered = result.data["registered"]
    legacy = result.data["legacy"]
    already = result.data["already_registered"]

    if registered:
        ui.success(f"Registered {len(registered)} key(s) under '{result.data['default_name']}'.")
        table, header = build_ssh_keys_table(registered)
        ui.info(header)
        ui.print(table)
    else:
        ui.info(f"Nothing to register. {already} local key(s) already match server-side keys.")

    if legacy:
        ui.warning(
            f"{len(legacy)} legacy auto-created key(s) named 'sdk-…' detected — "
            "you can delete these from the dashboard once your pods are stable."
        )
        table, header = build_ssh_keys_table(legacy)
        ui.dim(header)
        ui.print(table)
