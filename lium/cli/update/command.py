"""Update command implementation."""

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
from .actions import InstallJupyterAction


@click.command("update")
@click.argument("target")
@click.option("--jupyter", type=int, help="Install Jupyter Notebook on specified internal port")
@handle_errors
def update_command(target: str, jupyter: Optional[int]):
    """Update configuration of a running pod.

    \b
    TARGET: Pod identifier - can be:
      - Pod name/ID (eager-wolf-aa)
      - Index from 'lium ps' (1, 2, 3)

    \b
    Examples:
      lium update 1 --jupyter 8888          # Install Jupyter on pod #1
      lium update eager-wolf-aa --jupyter 8889  # Install Jupyter on specific pod
    """

    # Validate
    valid, error = validation.validate(target, jupyter)
    if not valid:
        raise CliFailure("invalid_arguments", error, EXIT_CONFIGURATION_ERROR)

    # Load data
    lium = Lium()
    all_pods = ui.load("Loading pods", lambda: lium.ps())

    if not all_pods:
        raise CliFailure("pod_not_found", "No active pods", EXIT_POD_NOT_FOUND)

    # Parse
    parsed, error = parsing.parse(target, all_pods)
    if error:
        raise CliFailure("pod_not_found", error, EXIT_POD_NOT_FOUND)

    pod = parsed.get("pod")

    # Execute
    ctx = {"lium": lium, "pod": pod, "port": jupyter}

    action = InstallJupyterAction()
    result = ui.load("Installing Jupyter Notebook", lambda: action.execute(ctx))

    if not result.ok:
        raise CliFailure(
            "jupyter_install_failed",
            result.error or "Failed to install Jupyter Notebook",
            EXIT_GENERAL_ERROR,
            hint=f"Check the pod is RUNNING in 'lium ps', then retry 'lium update {target} --jupyter {jupyter}'",
        )

    jupyter_url = result.data.get("jupyter_url")
    if jupyter_url:
        ui.info(f"Jupyter installed: {jupyter_url}")
