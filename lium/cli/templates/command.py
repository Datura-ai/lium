"""Templates command implementation."""

import json
from typing import List, Optional

import click

from lium.sdk import Lium, Template
from lium.cli import ui
from lium.cli.utils import handle_errors, resolve_output_format
from . import arch, display
from .actions import GetTemplatesAction


def _filter_arch(templates: List[Template], wanted: Optional[str]) -> List[Template]:
    if not wanted:
        return templates
    return [t for t in templates if arch.supports(wanted, arch.describe(t)["arch"])]


@click.command("templates", epilog="Use --format json for machine-readable output (includes template ids).")
@click.argument("search", required=False)
@click.option(
    "--format", "output_format",
    type=click.Choice(["table", "json"]),
    default="table",
    help="Output format. 'json' emits machine-readable JSON to stdout (suitable for piping to jq).",
)
@click.option("--json", "json_output", is_flag=True, hidden=True, help="Alias for --format json")
@click.option(
    "--arch", "wanted_arch", type=click.Choice(arch.ARCH_CHOICES),
    help="Only templates whose CUDA build runs on this GPU generation (Blackwell needs CUDA >= 12.8)",
)
@handle_errors
def templates_command(search: Optional[str], output_format: str, json_output: bool, wanted_arch: Optional[str]):
    """List available Docker templates and images.

    \b
    lium templates                       list every template
    lium templates pytorch               list templates matching a word
    lium templates --arch blackwell      only images whose CUDA build runs on B200/B300/RTX 50x0
    """
    output_format = resolve_output_format(output_format, json_output)

    lium = Lium()
    ctx = {"lium": lium, "search": search}

    action = GetTemplatesAction()
    if output_format == "json":
        result = action.execute(ctx)
    else:
        result = ui.load("Loading templates", lambda: action.execute(ctx))

    templates = _filter_arch(result.data["templates"], wanted_arch)

    if output_format == "json":
        click.echo(json.dumps([display.compact_template(t) for t in templates], indent=2, ensure_ascii=False))
        return

    if not templates:
        if wanted_arch:
            ui.warning(f"No templates whose CUDA build is known to run on {wanted_arch}")
            ui.dim("The generation is read from the image tag (cu128, cuda12.8, ...); tags without a CUDA version are excluded")
        else:
            ui.warning("No templates available")
        return

    table, header = display.build_templates_table(templates)
    ui.info(header)
    ui.print(table)
    if not wanted_arch:
        ui.dim("Runs on: read from the CUDA build in the tag; Blackwell (B200/B300/RTX PRO 6000/RTX 50x0) needs CUDA 12.8+. --arch filters on it.")
