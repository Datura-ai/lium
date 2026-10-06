"""Rsync command implementation."""

from typing import Optional
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import (
    CliFailure,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_POD_NOT_FOUND,
    handle_errors,
)
from . import validation, parsing
from .actions import RsyncPodsAction


@click.command("rsync")
@click.argument("targets")
@click.argument("local_path", type=click.Path(exists=True, readable=True))
@click.argument("remote_path", required=False)
@click.option("--bwlimit", type=click.IntRange(min=1), metavar="KIB_PER_S", help="Cap the transfer rate (rsync --bwlimit)")
@click.option("--exclude", multiple=True, metavar="PATTERN", help="Skip matching paths; repeatable (rsync --exclude)")
@click.option("--delete", is_flag=True, help="Remove files on the pod that are not in the local source")
@click.option("--progress", is_flag=True, help="Show overall transfer progress")
@handle_errors
def rsync_command(
    targets: str,
    local_path: str,
    remote_path: Optional[str],
    bwlimit: Optional[int],
    exclude: tuple,
    delete: bool,
    progress: bool,
):
    """Sync directories to GPU pods using rsync.

    Transfers resume where they stopped (rsync --partial), so an interrupted
    copy can simply be re-run.

    \b
    Examples:
      lium rsync my-pod ./data                        # -> /root/data on the pod
      lium rsync my-pod ./data /workspace/data
      lium rsync all ./src /workspace/src --exclude .git --exclude '*.pt'
      lium rsync my-pod ./ckpt /workspace/ckpt --bwlimit 20000 --progress
    \b
    Pod to pod: lium cp SRC_POD:/path DST_POD:/path
    """

    # Validate
    valid, error = validation.validate(local_path)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(targets, all_pods, local_path, remote_path)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    selected_pods = parsed.get("selected_pods")
    local_dir = parsed.get("local_dir")
    resolved_remote_path = parsed.get("remote_path")

    # Execute
    ctx = {
        "pods": selected_pods,
        "lium": lium,
        "local_dir": local_dir,
        "remote_path": resolved_remote_path,
        "rsync_kwargs": {
            "bwlimit": bwlimit,
            "exclude": list(exclude),
            "delete": delete,
            "progress": progress,
        },
    }

    action = RsyncPodsAction()
    result = action.execute(ctx)

    # Only error if anything failed
    if not result.ok:
        failed_huids = result.data.get("failed_huids", [])
        raise CliFailure(
            "rsync_failed",
            f"Failed to rsync to pods: {', '.join(failed_huids)}",
            EXIT_GENERAL_ERROR,
        )
