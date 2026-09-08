"""Describe command implementation."""

import json
import click

from lium.sdk import Lium
from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_POD_NOT_FOUND, ensure_config, handle_errors, resolve_output_format
from . import display
from .actions import pod_detail, pod_history, resolve_pod


@click.command("describe", epilog="Use --json (or --format json) for machine-readable output.")
@click.argument("pod_id")
@click.option("--json", "json_output", is_flag=True, help="Print the manifest as machine-readable JSON")
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    hidden=True,
    help="Alias for --json, matching `ps --format json`",
)
@handle_errors
def describe_command(pod_id: str, json_output: bool, output_format: str):
    """Show everything known about one pod: ports, GPU, template, billing, last event, node disk.

    \b
    A pod that is no longer listed can still be described by its id: the command
    prints the events the backend kept for it (the delete and its reason, a failed
    reboot's cause) instead of "not found".

    \b
    Examples:
      lium describe my-pod
      lium describe 1 --json | jq -r .access.ssh_command
    """
    json_output = resolve_output_format(output_format, json_output) == "json"

    # Only the human path may block on the interactive setup. A `--json` caller
    # is a script or an agent behind a pipe: it cannot answer a prompt, so a
    # missing key has to surface as the JSON error envelope Lium() raises.
    if not json_output:
        ensure_config()

    lium = Lium()
    pod = resolve_pod(lium, pod_id, show_progress=not json_output)

    if pod is None:
        events = pod_history(lium, pod_id)
        if not events:
            raise CliFailure("pod_not_found", f"Pod '{pod_id}' not found", EXIT_POD_NOT_FOUND)
        manifest = display.build_gone_manifest(pod_id, events)
        if json_output:
            click.echo(json.dumps(manifest, indent=2, ensure_ascii=False))
            return
        ui.warning(f"Pod {pod_id} is no longer listed. Its last event: {display.format_event(manifest['last_event'])}")
        ui.print(display.build_events_table(manifest))
        return

    manifest = display.build_manifest(pod, pod_detail(lium, pod.id))

    if json_output:
        click.echo(json.dumps(manifest, indent=2, ensure_ascii=False))
        return

    ui.print(display.build_manifest_table(manifest))
