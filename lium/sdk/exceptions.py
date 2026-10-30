"""Exception hierarchy for the Lium SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import GpuClockOutcome, GpuResetOutcome

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
    """Rate limit exceeded. ``retry_after`` is the response's ``Retry-After`` in seconds, ``None`` when absent."""

    retry_after: float | None = None


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
    (timeout, 5xx) and the by-name lookup then found fewer members than requested (none included), or could not
    list at all: the nodes may be rented. Either way a second ``up_cluster`` may rent a second
    cluster.

    Attributes:
        pod_ids: The member pod ids the API returned; empty when the order was not confirmed.
        listed: The member ids the last listing showed.
        confirmed: Whether the API confirmed the order.
        backup_notice: The rent answer's backup notice, if any: the members may run without backups.
    """

    def __init__(self, message: str, *, pod_ids=(), listed=(), confirmed: bool = True, backup_notice=None):
        super().__init__(message)
        self.backup_notice = backup_notice
        self.pod_ids = list(pod_ids)
        self.listed = list(listed)
        self.confirmed = confirmed


class LiumHostKeyError(LiumError):
    """A pod presented an SSH host key that differs from the pinned one."""


class LiumSSHError(LiumError):
    """SSH could not connect to a pod (refused, timed out, login rejected)."""


class OutputLimitExceeded(LiumError):
    """A command run with ``Lium.exec`` produced more than ``max_output_bytes`` bytes of output
    (stdout and stderr together). The limit bounds output bytes, not memory."""


class RemoteExecutionError(LiumError):
    """A function offloaded with ``@lium.machine`` did not return a result from the pod,
    or a command streamed with ``Lium.stream_exec(check=True)`` exited non-zero.

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
    ``lium topup``, ``lium topup card``) through that key is refused with this one error. The message
    names the window hit.
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


class LiumGpuOperationError(LiumError):
    """A GPU reset or clock change ran on the node and at least one GPU failed (a reset's status ``failed``, or a
    409, that lists ``gpus``).

    ``gpus`` is each GPU's outcome (:class:`GpuResetOutcome` from :meth:`Lium.reset_gpu`, :class:`GpuClockOutcome`
    from :meth:`Lium.lock_gpu_clocks` / :meth:`Lium.reset_gpu_clocks`), so the caller sees which GPUs failed and
    which stay locked; ``container_restarted`` says whether a reset started the pod's container again (``None``
    when the server did not say, always for clocks). ``code`` is the API's (``reset_not_supported``, ...).
    """

    def __init__(
        self,
        message: str,
        *,
        gpus: list[GpuResetOutcome | GpuClockOutcome] | None = None,
        container_restarted: bool | None = None,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.gpus: list[GpuResetOutcome | GpuClockOutcome] = list(gpus or [])
        self.container_restarted = container_restarted


class LiumCardTopUpError(LiumError):
    """A card top-up (``POST /payments/topup``, :meth:`Lium.topup_card`) did not charge (402).

    ``code`` says why: ``CARD_AUTHENTICATION_REQUIRED`` (the bank wants the cardholder to confirm
    with 3-D Secure, see below), ``CARD_AUTHENTICATION_NOT_COMPLETED`` (the cardholder failed or
    abandoned that confirmation), ``CARD_AUTHENTICATION_UNAVAILABLE`` (3-D Secure cannot run for
    this card), ``CARD_AUTHENTICATION_EXPIRED`` (not confirmed within 7 days, canceled),
    ``CARD_DECLINED`` (``decline_code`` is the bank's reason), ``NO_SAVED_CARD``,
    ``NO_DEFAULT_CARD``, or ``WALLET_NOT_SUPPORTED`` (the saved method is Link, Apple Pay or
    Google Pay). A key budget refusal is not this class. The message is the server's plain
    sentence; the balance did not change.

    ``idempotency_key`` is the key the call was sent with (yours, or the one the SDK made), and
    ``transaction_id`` the platform's row for it when the server named one.

    A ``CARD_AUTHENTICATION_REQUIRED`` **with** ``next_action_url`` (``is_step_up``) is not final:
    the payment is open. The cardholder confirms at ``next_action_url`` and the balance is credited
    with no further call. Repeat only with the **same** ``idempotency_key`` (it answers the same URL
    until the cardholder confirms, then the charge); a new key opens a second payment on the card.
    Without ``next_action_url`` it is final: top up once by card at ``dashboard_url``, then retry
    with a new key. Every other code is final too: retry with a new key once the cause is fixed.
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
        transaction_id: str | None = None,
        next_action_url: str | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.status = status
        self.decline_code = decline_code
        self.dashboard_url = dashboard_url
        self.payment_intent_id = payment_intent_id
        self.transaction_id = transaction_id
        self.next_action_url = next_action_url
        self.idempotency_key = idempotency_key

    @property
    def is_step_up(self) -> bool:
        """True for a 3-D Secure step-up: the payment is open until the cardholder confirms at
        ``next_action_url``; repeat only with the same ``idempotency_key``."""
        return self.code == "CARD_AUTHENTICATION_REQUIRED" and bool(self.next_action_url)


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

class LiumRentOutcomeUnknownError(LiumError):
    """A rent (:meth:`Lium.up`) was posted and its outcome could not be established: the first request got
    no answer (a timeout, a dropped connection, a 5xx), no new pod was listed yet, and the repeat was refused
    or failed too. The server may still be completing the first request, so a pod **may exist and bill**:
    check ``ps()`` before renting again. ``idempotency_key`` is the key both requests carried.
    """

    def __init__(
        self,
        message: str,
        *,
        idempotency_key: str | None = None,
        pod_name: str | None = None,
        executor_id: str | None = None,
        candidates: list[str] | None = None,
        endpoint: str | None = None,
        payload: dict | None = None,
        code: str | None = None,
        hint: str | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message, code=code, hint=hint, request_id=request_id)
        self.idempotency_key = idempotency_key
        self.pod_name = pod_name
        self.executor_id = executor_id
        # new pods with the rent's name, found after the answer was lost: maybe this rent's, maybe another client's
        self.candidates = list(candidates or [])
        # the rent request, kept for the MCP server's guarded replay with the same key
        self.endpoint = endpoint
        self.payload = payload


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
    "LiumRentOutcomeUnknownError",
]
