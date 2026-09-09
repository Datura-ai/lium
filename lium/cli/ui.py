"""UI library - thin wrapper around Rich for user interaction.

This module provides a clean interface to Rich, handling only I/O operations.
All formatting and domain logic should live in command-specific modules.
"""

import os
from typing import Callable, Optional, TypeVar, List
from contextlib import contextmanager
from rich.prompt import Confirm, Prompt
from rich.table import Table

from lium.cli.interactive import is_interactive, noninteractive_reason
from lium.cli.utils import (
    EXIT_CONFIGURATION_ERROR,
    CliFailure,
    console,
    loading_status,
    notice_console,
)

T = TypeVar("T")


def is_debug() -> bool:
    """Check if debug mode is enabled via LIUM_DEBUG env var."""
    return os.environ.get("LIUM_DEBUG", "").strip() in ("1", "true", "True", "TRUE")


# ============================================================================
# Loading & Status
# ============================================================================

@contextmanager
def loading(message: str):
    """Context manager for loading status.

    Example:
        with ui.loading("Loading pods"):
            pods = lium.ps()
    """
    with loading_status(message, ""):
        yield


def load(message: str, fn: Callable[[], T]) -> T:
    """Execute a function with loading status.

    Args:
        message: Loading message to display
        fn: Function to execute

    Returns:
        Result of the function

    Example:
        pods = ui.load("Loading pods", lambda: lium.ps())
    """
    with loading_status(message, ""):
        return fn()


# ============================================================================
# User Input
# ============================================================================

def confirm(message: str, default: bool = False, *, hint: str = "re-run with --yes", stderr: bool = False) -> bool:
    """Ask user for yes/no confirmation.

    Never blocks a caller that cannot answer. When stdin is not a terminal or
    ``LIUM_NONINTERACTIVE`` is set, the question is not asked: the command fails
    with ``confirmation_required`` and a hint naming the flag that skips the
    prompt. Silently taking the default would either do the risky thing
    unasked or exit 0 having done nothing — both read as success to a script.

    Args:
        message: Question to ask
        default: Default answer if user just presses enter
        hint: What a non-interactive caller should do instead of answering
        stderr: Ask on stderr — for a command whose stdout is a JSON document

    Returns:
        True if confirmed, False otherwise

    Raises:
        CliFailure: when no one can answer the prompt, or the terminal went
            away mid-prompt.
    """
    if not is_interactive():
        raise CliFailure(
            "confirmation_required",
            f"Confirmation required: {message} "
            f"(no prompt shown because {noninteractive_reason()}; {hint})",
            EXIT_CONFIGURATION_ERROR,
        )
    try:
        if stderr:
            return Confirm.ask(message, default=default, console=notice_console())
        return Confirm.ask(message, default=default)
    except EOFError:
        # The terminal went away mid-prompt. No answer is not a yes.
        raise CliFailure(
            "confirmation_required",
            f"No answer to: {message} ({hint})",
            EXIT_CONFIGURATION_ERROR,
        )


def prompt(message: str, default: Optional[str] = None, *, hint: Optional[str] = None) -> str:
    """Prompt user for text input.

    Without a terminal the prompt is not shown: a ``default`` is returned as
    is, and a required value fails with ``input_required`` plus ``hint`` (the
    option to pass instead), so a piped caller never waits on a question it
    cannot see.

    Args:
        message: Prompt message
        default: Value used when the user just presses Enter, or when no
            terminal is attached
        hint: What a non-interactive caller should pass instead

    Returns:
        User's input

    Raises:
        CliFailure: when the value is required and no one can type it.
    """
    if not is_interactive():
        if default is not None:
            return default
        raise CliFailure(
            "input_required",
            f"Input required: {message} "
            f"(no prompt shown because {noninteractive_reason()}"
            f"{'; ' + hint if hint else ''})",
            EXIT_CONFIGURATION_ERROR,
        )
    try:
        if default is None:
            return Prompt.ask(message)
        return Prompt.ask(message, default=default)
    except EOFError:
        if default is not None:
            return default
        raise CliFailure(
            "input_required",
            f"No answer to: {message}{'; ' + hint if hint else ''}",
            EXIT_CONFIGURATION_ERROR,
        )


# ============================================================================
# Messages
# ============================================================================

def success(message: str) -> None:
    """Display success message (green)."""
    console.success(message)


def error(message: str) -> None:
    """Display error message (red)."""
    console.error(message)


def warning(message: str) -> None:
    """Display warning message (yellow)."""
    console.warning(message)


def info(message: str) -> None:
    """Display info message (cyan/blue)."""
    console.info(message)


def notice(message: str) -> None:
    """Display an informational startup notice on stderr."""
    notice_console().info(message)


def notice_warning(message: str) -> None:
    """Display a warning startup notice on stderr."""
    notice_console().warning(message)


def notice_debug(message: str) -> None:
    """Display a debug startup notice on stderr, only if LIUM_DEBUG=1."""
    if is_debug():
        notice_console().dim(f"[DEBUG] {message}")


def dim(message: str) -> None:
    """Display dimmed/secondary text."""
    console.dim(message)


def print(*args, **kwargs) -> None:
    """Display plain message."""
    console.print(*args, **kwargs)


def debug(message: str) -> None:
    """Display debug message only if LIUM_DEBUG=1."""
    if is_debug():
        console.dim(f"[DEBUG] {message}")


# ============================================================================
# Tables
# ============================================================================

def table(headers: List[str], rows: List[List[str]], **kwargs) -> None:
    """Display a table.

    Args:
        headers: Column headers
        rows: List of rows (each row is a list of values)
        **kwargs: Additional arguments passed to Rich Table

    Example:
        ui.table(
            ["Name", "Status", "Price"],
            [
                ["pod-1", "running", "$0.50/h"],
                ["pod-2", "stopped", "$0.30/h"],
            ]
        )
    """
    rich_table = Table(**kwargs)

    for header in headers:
        rich_table.add_column(header)

    for row in rows:
        rich_table.add_row(*row)

    console.print(rich_table)


# ============================================================================
# Styling Helpers
# ============================================================================

def styled(text: str, style: str) -> str:
    """Get styled text using console theme.

    Args:
        text: Text to style
        style: Style key from theme (success, error, warning, info, dim, etc.)

    Returns:
        Styled text string
    """
    return console.get_styled(text, style)
