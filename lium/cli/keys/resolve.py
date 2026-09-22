"""`--key <name|id>` on `lium ps` and `lium billing history`: the API key id the server filters by, and the
check that it did."""

import re
from typing import Any, Callable, Iterable, List, Tuple, TypeVar

import click

from lium.cli import ui
from lium.sdk import Lium
from lium.sdk.exceptions import LiumSessionError

Row = TypeVar("Row")
NO_KEY_FILTER = (
    "This server cannot filter by API key (its rows carry no api_key_id): showing every {what} of the account, "
    "not one key's"
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
NAME_NEEDS_SESSION = (
    "A key name is looked up on the session-only key list: run `lium workspaces login` "
    "(or set LIUM_SESSION_TOKEN), or pass the key id from `lium keys list`"
)
# What ``key_of`` returns for a row that carries no api-key field at all (a server before per-key pods), as
# opposed to ``None`` for a row the server stamped ``null`` (a browser rental) — the two must not be confused:
# prod already sends ``created_by_api_key_id: null``, so an account with no key-rented pods must read as
# "the server filtered: nothing", not "the server cannot filter"
UNSTAMPED = object()


def key_id_for(lium: Lium, name_or_id: str) -> str:
    """The id behind ``--key``: an id is passed through; a name is resolved on ``GET /keys`` in the key's own
    workspace, which needs a session (``session_required``, exit 3, without one — the message says to pass
    the id instead)."""
    if _UUID_RE.match(name_or_id):
        return name_or_id
    if not lium.workspaces.session_token:
        raise LiumSessionError(NAME_NEEDS_SESSION)
    current = lium.workspaces.current()
    return lium.api_keys.resolve(name_or_id, current.id if current else None).id


def rented_through(rows: Iterable[Row], api_key_id: str, key_of: Callable[[Row], Any]) -> Tuple[List[Row], bool]:
    """``(the rows rented through the key, whether the server could tell)``.

    ``key_of`` reads a row's api-key field: its value (``None`` when the server stamped ``null`` — a browser
    rental), or ``UNSTAMPED`` when the row carries no such field. The check is on presence, never truthiness:
    a ``null``, ``0`` or ``""`` stamp is a server that knows the field, and a whole account of ``null`` stamps
    is a key that rented nothing — ``([], True)``. Only rows that all lack the field are a server before
    per-key pods, which ignored the query and answered the whole account — those rows come back untouched
    with ``False``, so the caller says so instead of labelling the account's figures as one key's. No rows at
    all is ``([], True)``: nothing to mislabel."""
    rows = list(rows)
    if not rows:
        return [], True
    stamps = [key_of(row) for row in rows]
    if all(stamp is UNSTAMPED for stamp in stamps):
        return rows, False
    kept = [row for row, stamp in zip(rows, stamps) if stamp is not UNSTAMPED and stamp is not None and str(stamp) == api_key_id]
    return kept, True


def say_unfiltered(what: str, json_output: bool) -> None:
    """The one line for a server that could not filter by key: stderr under JSON, a warning otherwise."""
    line = NO_KEY_FILTER.format(what=what)
    if json_output:
        click.echo(line, err=True)
    else:
        ui.warning(line)
