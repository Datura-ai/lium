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
        cause: The failure the backend recorded for the pod (the error
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


class ClusterNotListedError(LiumError):
    """The cluster rent may have gone through, but the pod listing did not show a whole
    cluster within the lookup window.

    Two ways in. ``confirmed`` is True when the rent route confirmed the order and named the
    member pods (``pod_ids``) but the listing did not show every one of them, or failed: the
    nodes are rented and billing. ``confirmed`` is False when the rent route gave no answer
    (timeout, 5xx) and the by-name lookup then found fewer members than requested, or could not
    list at all: the nodes may be rented. Either way a second ``up_cluster`` may rent a second
    cluster.

    Attributes:
        pod_ids: The member pod ids the API returned; empty when the order was not confirmed.
        listed: The member ids the last listing showed.
        confirmed: Whether the API confirmed the order.
    """

    def __init__(self, message: str, *, pod_ids=(), listed=(), confirmed: bool = True):
        super().__init__(message)
        self.pod_ids = list(pod_ids)
        self.listed = list(listed)
        self.confirmed = confirmed


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


class LiumScopeError(LiumPermissionError):
    """The API key lacks the scope the route needs (403 ``API key '<name>' does not have the '<scope>' scope``).

    ``scope`` is the missing scope (``read``, ``rent``, ``manage``, ``billing``) when the server named it,
    else ``None``. Another key with that scope fixes this, not funds or verification.
    """

    def __init__(
        self,
        message: str,
        *,
        scope: str | None = None,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.scope = scope


class LiumBudgetExceededError(LiumPermissionError):
    """The API key's budget refused the request (402, ``error.code`` ``API_KEY_BUDGET_EXCEEDED``).

    A per-key daily, monthly or lifetime budget (``lium keys create --daily-budget / --monthly-budget /
    --max-budget``, ``lium keys budget``) is reached: the billing tick deletes the key's pods (data outside
    a volume is lost), and a rent, a pod extend / schedule change or a key-initiated top-up (``lium fund``,
    ``lium topup``, ``lium topup card``) through that key is refused with this one error (server support
    pending). The message names the window hit.
    ``budget_usd``, ``spent_usd``, ``window`` (``daily``, ``monthly`` or ``max``) and ``api_key_id`` are filled when the
    server's error body carried them, else ``None``. A :class:`LiumPermissionError` handler keeps working; this
    class is for callers that want the numbers.
    """

    def __init__(
        self,
        message: str,
        *,
        budget_usd: float | None = None,
        spent_usd: float | None = None,
        window: str | None = None,
        api_key_id: str | None = None,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.budget_usd = budget_usd
        self.spent_usd = spent_usd
        self.window = window
        self.api_key_id = api_key_id


class LiumCardTopUpError(LiumError):
    """A card top-up (``POST /payments/topup``, :meth:`Lium.topup_card`) did not charge (402).

    ``code`` says why: ``CARD_AUTHENTICATION_REQUIRED`` (the bank wants a one-time confirmation
    the API cannot show — top up once by card at ``dashboard_url``, then retry), ``CARD_DECLINED``
    (``decline_code`` is the bank's reason), ``NO_SAVED_CARD``, or ``NO_DEFAULT_CARD``. A key
    budget refusal is not this class. The message is the server's plain sentence; the balance
    did not change.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
        status: str | None = None,
        decline_code: str | None = None,
        dashboard_url: str | None = None,
        payment_intent_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.status = status
        self.decline_code = decline_code
        self.dashboard_url = dashboard_url
        self.payment_intent_id = payment_intent_id


class LiumChargeOutcomeUnknownError(LiumError):
    """A card top-up (:meth:`Lium.topup_card`) was posted and the answer was lost — a timeout, a
    dropped connection, or a 5xx after the platform had already asked Stripe to charge. Stripe
    charges the card before it answers, so the charge **may have gone through**: check the balance
    (or the transactions) before trying again. A repeat with the same ``idempotency_key`` returns the
    first charge, and the card is charged once; the key is on the exception.
    """

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.idempotency_key = idempotency_key

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
    "LiumScopeError",
    "LiumBudgetExceededError",
    "RemoteExecutionError",
    "LiumCardTopUpError",
    "LiumChargeOutcomeUnknownError",
]
