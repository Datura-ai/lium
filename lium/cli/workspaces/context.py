"""The workspace a command acts in, as one line under its output (lium-platform DAH-3030)."""

from typing import Optional

import requests
from rich.markup import escape

from lium.cli import ui
from lium.cli.utils import CliFailure, EXIT_CONFIGURATION_ERROR
from lium.sdk import Lium
from lium.sdk.exceptions import LiumError
from lium.sdk.models import WorkspaceInfo


def context_line(workspace: WorkspaceInfo) -> str:
    """``Workspace: Research (member)`` — ``· personal`` for the account's own workspace.

    The name is the owner's text: escaped, so ``[EU]`` in a name is not read as console markup.
    """
    line = f"Workspace: {escape(workspace.name)} ({workspace.role})"
    if workspace.is_personal:
        line += " · personal"
    return line


def acts_elsewhere(workspace: WorkspaceInfo, requested: Optional[str], requested_id: Optional[str]) -> bool:
    """Whether the key acts in another workspace than the one ``lium workspaces use`` / ``--workspace`` named.

    Compared by id when ``[workspace.<name>] id`` was saved (a rename on the server changes nothing),
    by name otherwise (a stored default with no saved key).
    """
    if not requested:
        return False
    if requested_id:
        return workspace.id != requested_id
    return not workspace.matches(requested)


def show_workspace(lium: Lium, acting: bool = False, on_stderr: bool = False) -> None:
    """Print the workspace line (under ``ps`` / ``ls`` output, before ``up`` / ``rm`` act); nothing on a
    server without workspaces. With ``on_stderr=True`` (a command whose stdout is one JSON document)
    the line and the read-failure warning go to stderr, so ``… --format json | jq`` still parses.

    The line is context, not the command: a ``GET /users/me`` that fails is reported as a warning with
    the server's reason and the command goes on with the key it has. When the workspace named by
    ``lium workspaces use`` / ``--workspace`` is not the one the key acts in, the line is a warning that
    says so. With ``acting=True`` (``up`` / ``rm``: a rental or a removal follows) and an explicit
    ``--workspace`` / LIUM_WORKSPACE, both cases are refusals instead (exit 2): a key that acts elsewhere,
    and a key whose workspace could not be read — nothing is rented or removed in a workspace the user
    did not name, and nothing is rented or removed unverified.
    """
    try:
        workspace = lium.workspaces.current()
    except (LiumError, requests.RequestException) as e:
        if acting and lium.config.workspace_explicit:
            raise CliFailure(
                "workspace_unverified",
                f"Not acting in '{lium.config.workspace}': GET /users/me failed ({e}), so the saved key's workspace "
                "could not be checked; retry, or run `lium workspaces` to see the full error",
                EXIT_CONFIGURATION_ERROR,
            )
        (ui.notice_warning if on_stderr else ui.warning)(
            f"Workspace not shown — GET /users/me failed: {escape(str(e))}. Run `lium workspaces` to see the full error."
        )
        return
    if workspace is None:
        return
    requested, requested_id = lium.config.workspace, lium.config.workspace_id
    if not acts_elsewhere(workspace, requested, requested_id):
        (ui.notice if on_stderr else ui.dim)(context_line(workspace))
        return
    which_key = f"the key saved for '{requested}'" if requested_id else "the key this command ran with"
    mismatch = (
        f"{which_key} acts in {workspace.name}, not in '{requested}'; "
        f"run `lium keys create <name> --workspace {requested} --save` to get one that does"
    )
    if acting and lium.config.workspace_explicit:
        # handle_errors escapes the message once before rendering
        raise CliFailure("workspace_mismatch", f"Not acting in '{requested}': {mismatch}", EXIT_CONFIGURATION_ERROR)
    (ui.notice_warning if on_stderr else ui.warning)(f"{context_line(workspace)} — {escape(mismatch)}")
