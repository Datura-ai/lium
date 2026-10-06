"""Whether the CLI is allowed to stop and ask the user a question.

A prompt only makes sense when a human can answer it. An agent driving the CLI
through a pipe cannot: a hidden ``Confirm.ask`` waits on a stdin nobody writes
to, and the command looks hung. Every prompt in the renter CLI goes through
:func:`is_interactive` before it asks anything.

This module has no CLI imports on purpose: ``lium.cli.settings`` needs it, and
``lium.cli.utils`` imports ``settings`` at module load.
"""

import os
import sys

_TRUTHY = frozenset({"1", "true", "yes", "on"})

NONINTERACTIVE_ENV = "LIUM_NONINTERACTIVE"


def noninteractive_requested() -> bool:
    """``LIUM_NONINTERACTIVE=1`` (or true/yes/on) forbids prompting even on a terminal."""
    return os.environ.get(NONINTERACTIVE_ENV, "").strip().lower() in _TRUTHY


def stdin_is_terminal() -> bool:
    """Whether stdin is attached to a terminal a person can type into."""
    stream = sys.stdin
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        # A closed or replaced stdin cannot answer anything either.
        return False


def is_interactive() -> bool:
    """True only when a prompt can be answered: a terminal on stdin and no opt-out."""
    return not noninteractive_requested() and stdin_is_terminal()


def noninteractive_reason() -> str:
    """Why prompting is off — for the error a refused prompt produces."""
    if noninteractive_requested():
        return f"{NONINTERACTIVE_ENV} is set"
    return "stdin is not a terminal"


__all__ = [
    "NONINTERACTIVE_ENV",
    "is_interactive",
    "noninteractive_reason",
    "noninteractive_requested",
    "stdin_is_terminal",
]
