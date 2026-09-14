"""Exception hierarchy for the Lium SDK."""

class LiumError(Exception):
    """Base exception for Lium SDK.

    ``code``, ``hint`` and ``request_id`` come from the API's error envelope
    (``error: {code, message, hint, request_id}``) and the ``X-Request-Id``
    header; all three are ``None`` when the server did not send them.
    """

    def __init__(self, message: str = "", *, code: str | None = None, hint: str | None = None,
                 request_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.request_id = request_id


class LiumAuthError(LiumError):
    """Authentication error."""


class LiumSessionError(LiumAuthError):
    """A browser session (``lium workspaces login`` / LIUM_SESSION_TOKEN) is missing or refused.

    An API key cannot fix this, so the CLI's hint must not point at one.
    """


class LiumRateLimitError(LiumError):
    """Rate limit exceeded."""


class LiumServerError(LiumError):
    """Server error."""


class LiumNotFoundError(LiumError):
    """Resource not found (404)."""


class LiumPermissionError(LiumError):
    """The account is not allowed to do this (403)."""


class PodStartError(LiumError):
    """A pod reached a state from which it will never become ready.

    Raised by :meth:`Lium.wait_ready` (and everything built on it) when the pod
    reports a terminal status such as ``FAILED`` or ``STOPPED``, or disappears
    from the account's pod list while being waited for. A timeout is *not* a
    start error: a pod that is merely slow is still returned as ``None``.

    Attributes:
        pod_id: The id that was waited for.
        pod: The last ``PodInfo`` seen for it, or ``None`` if it was never listed.
        status: The last status seen (upper-cased), or ``None`` if never listed.
        history: Every distinct status observed while waiting, in order.
        cause: The failure the backend recorded for the pod (the validator's
            headline, e.g. ``Container creation failed due to ... (failure_step:
            ssh_connect)``), or ``None`` when it recorded nothing readable.
    """

    def __init__(self, message: str, *, pod_id: str, pod=None, status=None, history=None, cause=None):
        super().__init__(message)
        self.pod_id = pod_id
        self.pod = pod
        self.status = status
        self.history = list(history or [])
        self.cause = cause


class LiumHostKeyError(LiumError):
    """A pod presented an SSH host key that differs from the pinned one."""


class RemoteExecutionError(LiumError):
    """A function offloaded with ``@lium.machine`` did not return a result from the pod.

    When the function raised, the caller sees the original exception type and this
    error is its ``__cause__``; ``remote_traceback`` is the traceback from the pod.
    """

    def __init__(
        self,
        message: str,
        *,
        exception_type: str | None = None,
        remote_traceback: str = "",
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ):
        super().__init__(message)
        self.exception_type = exception_type
        self.remote_traceback = remote_traceback
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class LiumInsufficientBalanceError(LiumPermissionError):
    """The account cannot pay for this (403 with a balance reason).

    ``required`` and ``available`` are USD amounts when the server said what
    they are, else ``None``. Callers that catch :class:`LiumPermissionError`
    keep working; ones that want the numbers catch this class.
    """

    def __init__(
        self,
        message: str,
        *,
        required: float | None = None,
        available: float | None = None,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.required = required
        self.available = available


__all__ = [
    "LiumError",
    "LiumAuthError",
    "LiumRateLimitError",
    "LiumServerError",
    "LiumNotFoundError",
    "LiumPermissionError",
    "PodStartError",
    "LiumHostKeyError",
    "LiumInsufficientBalanceError",
    "RemoteExecutionError",
]
