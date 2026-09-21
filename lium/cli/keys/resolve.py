"""`--key <name|id>` on `lium ps` and `lium billing history`: the API key id the server filters by, and the
check that it did."""

import re
from typing import Callable, Iterable, List, Optional, Tuple, TypeVar

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


def rented_through(rows: Iterable[Row], api_key_id: str, key_of: Callable[[Row], Optional[str]]) -> Tuple[List[Row], bool]:
    """``(the rows rented through the key, whether the server could tell)``.

    A server that stamps its rows with ``api_key_id`` filtered on the query (or is filtered here, should it
    have ignored it); a server before per-key pods stamps nothing and answers the whole account — those rows
    come back untouched with ``False``, so the caller says so instead of labelling the account's figures as
    one key's. No rows at all is ``([], True)``: nothing to mislabel."""
    rows = list(rows)
    if not rows:
        return [], True
    if not any(key_of(row) for row in rows):
        return rows, False
    return [row for row in rows if key_of(row) == api_key_id], True


def say_unfiltered(what: str, json_output: bool) -> None:
    """The one line for a server that could not filter by key: stderr under JSON, a warning otherwise."""
    line = NO_KEY_FILTER.format(what=what)
    if json_output:
        click.echo(line, err=True)
    else:
        ui.warning(line)
