"""Start a command on a pod in the background and come back at once.

The one home for the launcher line that ``lium exec --detach`` (CLI) and the
SDK's detached exec send to a pod, so the two never drift apart.
"""

from __future__ import annotations

import posixpath
import secrets
import shlex
import time
from typing import Optional

DEFAULT_DETACH_LOG_DIR = "/workspace/logs"

# ``echo $!`` prints a PID as soon as the shell forks, whether or not the child
# manages to exec, so both programs the launcher needs are checked first. A
# failed check writes to stderr only and exits before anything is started; the
# caller then sees "no PID" plus the reason instead of a false "Started". The
# trailing ``&&`` chains the check to the ``mkdir … || exit 1`` that follows, so
# anything ``&&``-ed in front of the launcher (the ``eval "$(cat)"`` with which
# ``Lium.exec`` applies the stdin exports) failing stops the launch too.
_PREFLIGHT = (
    'for t in setsid bash; do command -v "$t" >/dev/null 2>&1 || '
    '{ echo "$t not found on the pod" >&2; exit 127; }; done && '
)


def detach_token(now: Optional[float] = None) -> str:
    """``<UTC stamp>-<6 hex>`` naming one launch.

    The random tail keeps two jobs started in the same second from sharing a
    log (or script) file.
    """
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)) + "-" + secrets.token_hex(3)


def default_detach_log_path(token: str) -> str:
    return f"{DEFAULT_DETACH_LOG_DIR}/exec-{token}.log"


def build_detached_command(command: str, log_path: str) -> str:
    """The remote command line that starts ``command`` in the background and prints its PID.

    ``nohup`` alone is not enough over a non-interactive SSH session: the child
    keeps the session's stdin and its process group, so ``exec`` blocks until
    the child exits, and a closed session can still take the child down.
    ``setsid`` gives it its own session, ``< /dev/null`` detaches stdin, and both
    output streams go to the log file. ``echo $!`` is the only thing the caller
    reads back on stdout.
    """
    log_dir = posixpath.dirname(log_path) or "."
    return (
        _PREFLIGHT
        + f"mkdir -p {shlex.quote(log_dir)} || exit 1; "
        f"nohup setsid bash -lc {shlex.quote(command)} "
        f"> {shlex.quote(log_path)} 2>&1 < /dev/null & echo $!"
    )
