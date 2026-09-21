"""`--key <name|id>` on `lium ps` and `lium billing history`: the API key id the server filters by."""

import re

from lium.sdk import Lium
from lium.sdk.exceptions import LiumSessionError

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
