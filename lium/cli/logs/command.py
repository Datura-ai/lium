"""Logs command implementation."""

import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import handle_errors, ensure_config
from . import validation, parsing
from .actions import StreamLogsAction


@click.command("logs")
@click.argument("pod_id", required=True)
@click.option("--tail", "-n", default=100, help="Number of lines to show from the end of the logs")
@click.option("--follow", "-f", is_flag=True, help="Follow log output")
@handle_errors
def logs_command(pod_id: str, tail: int, follow: bool):
    """Stream logs from a pod.

    Examples:

        lium logs abc123              # Show last 100 lines

        lium logs abc123 -n 50        # Show last 50 lines

        lium logs abc123 -f           # Follow logs in real-time

        lium logs abc123 -f -n 10     # Follow with 10 lines of history
    """
    ensure_config()

    # Validate
    valid, error = validation.validate(pod_id, tail)
    if not valid:
        ui.error(error)
        return

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        ui.warning("No active pods")
        return

    # Parse
    parsed, error = parsing.parse(pod_id, all_pods)
    if error:
        ui.error(error)
        return

    pod = parsed["pod"]

    # Execute
    if follow:
        ui.dim(f"Streaming logs from {pod.name or pod.huid}... (Ctrl+C to stop)")

    ctx = {"lium": lium, "pod": pod, "tail": tail, "follow": follow}
    action = StreamLogsAction()

    try:
        for line in action.execute(ctx):
            click.echo(line)
    except KeyboardInterrupt:
        if follow:
            ui.dim("\nStopped following logs")
