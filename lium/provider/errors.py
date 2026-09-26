"""Error taxonomy for the provider SDK.

Every external failure surface (portal HTTP, SSH, wallet materialisation) is
mapped onto a stable code with an actionable hint, so an agent driving the
CLI can branch on machine-readable values rather than log strings.

Exit-code mapping (used by ``lium/cli/provider/_render.py``). The UPPER_CASE
codes keep the old provider map while scripts migrate:

    0  success
    1  user error (bad arg)
    2  auth error, or a valid token refused for this hotkey (PORTAL_FORBIDDEN)
    3  portal error (server-side, not auth)
    5  SSH error
    6  config error
    7  token-cache contention (PORTAL_AUTH_REFRESH_RACE)

A namespaced snake_case code (``input.confirmation_required``, ``net.unreachable``,
``portal.<the portal's detail.code>``) exits by the unified map instead
(:func:`unified_exit_code`, ``docs/exit-codes.md``).

Each error code is exported as a string constant so callers can do::

    from lium.provider.errors import HOTKEY_NOT_REGISTERED
    if err.code == HOTKEY_NOT_REGISTERED:
        ...
"""

from __future__ import annotations

from typing import Any

# --- Error codes ---------------------------------------------------------

# Auth
WALLET_NOT_FOUND = "WALLET_NOT_FOUND"
HOTKEY_NOT_REGISTERED = "HOTKEY_NOT_REGISTERED"
PORTAL_AUTH_EXPIRED = "PORTAL_AUTH_EXPIRED"
PORTAL_AUTH_INVALID = "PORTAL_AUTH_INVALID"
PORTAL_AUTH_REFRESH_RACE = "PORTAL_AUTH_REFRESH_RACE"
PORTAL_FORBIDDEN = "PORTAL_FORBIDDEN"

# Portal
PORTAL_CONTRACT_DRIFT = "PORTAL_CONTRACT_DRIFT"
PORTAL_NOT_FOUND = "PORTAL_NOT_FOUND"
PORTAL_SERVER_ERROR = "PORTAL_SERVER_ERROR"
PORTAL_RATE_LIMIT = "PORTAL_RATE_LIMIT"
PORTAL_REQUEST_REJECTED = "PORTAL_REQUEST_REJECTED"

# SSH / install
SSH_UNREACHABLE = "SSH_UNREACHABLE"
SSH_AUTH_FAILED = "SSH_AUTH_FAILED"
INSTALLER_PARTIAL_FAIL = "INSTALLER_PARTIAL_FAIL"
EXECUTOR_UUID_MISMATCH = "EXECUTOR_UUID_MISMATCH"
UUID_NOT_FOUND = "UUID_NOT_FOUND"

# Config / args
PORTS_INVALID = "PORTS_INVALID"
ARG_INVALID = "ARG_INVALID"
CONFIG_MISSING = "CONFIG_MISSING"

# Namespaced codes (snake_case, never renamed once shipped). A portal refusal that names its own
# ``detail.code`` is raised as ``portal.<that code>``.
INPUT_REQUIRED = "input.input_required"
CONFIRMATION_REQUIRED = "input.confirmation_required"
INTERRUPTED = "input.interrupted"
NET_UNREACHABLE = "net.unreachable"
PORTAL_NOT_SUPPORTED = "portal.not_supported"
NODE_NOT_LISTED = "node.not_listed_yet"
HANDOFF_REQUIRED = "human.handoff_required"
HANDOFF_EXPIRED = "human.handoff_expired"
API_TOKEN_NEEDS_SESSION = "portal.api_token_needs_session"
API_TOKEN_SCOPE_MISSING = "portal.api_token_scope_missing"
OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT = "portal.overview_not_for_custodied_account"

# The unified exit map (docs/exit-codes.md).
EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_INPUT = 2
EXIT_API = 3
EXIT_NETWORK = 4
EXIT_NOT_FOUND = 5
EXIT_AUTH = 6
EXIT_RETRYABLE = 7
EXIT_BLOCKED = 10
EXIT_NOT_LISTED = 11
EXIT_HUMAN = 12
EXIT_INTERRUPTED = 130

_NAMESPACE_EXITS: dict[str, int] = {
    "input": EXIT_INPUT,
    "human": EXIT_HUMAN,
    "auth": EXIT_AUTH,
    "net": EXIT_NETWORK,
    "ssh": EXIT_NETWORK,
    "host": EXIT_GENERAL,
    "portal": EXIT_API,
}


def unified_exit_code(code: str, status: int | None = None) -> int:
    """The unified-map exit status of a namespaced code; ``status`` is the portal's HTTP status, if any."""
    if code == INTERRUPTED:
        return EXIT_INTERRUPTED
    if code.startswith("node.blocked"):
        return EXIT_BLOCKED
    if code == NODE_NOT_LISTED:
        return EXIT_NOT_LISTED
    if code == PORTAL_NOT_SUPPORTED:
        return EXIT_API
    if code in (API_TOKEN_NEEDS_SESSION, API_TOKEN_SCOPE_MISSING, OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT):
        return EXIT_AUTH
    leaf = code.rsplit(".", 1)[-1]
    not_found = leaf == "not_found" or leaf.endswith("_not_found")
    if code.startswith("portal."):
        if status in (401, 403, 419, 440):
            return EXIT_AUTH
        if status == 404 or not_found:
            return EXIT_NOT_FOUND
        if status == 429 or leaf == "rate_limited":
            return EXIT_RETRYABLE
        return EXIT_API
    if not_found:
        return EXIT_NOT_FOUND
    return _NAMESPACE_EXITS.get(code.split(".", 1)[0], EXIT_GENERAL)


# Default hint table -- keep human and short. Empty string => no hint.
_HINTS: dict[str, str] = {
    WALLET_NOT_FOUND: "Run `btcli wallet new_coldkey` then `btcli wallet new_hotkey`, or check --coldkey/--hotkey names.",
    HOTKEY_NOT_REGISTERED: "Run `btcli subnet register --netuid 51 --wallet.name <coldkey> --wallet.hotkey <hotkey>` first.",
    PORTAL_AUTH_EXPIRED: "Run `lium provider portal login` to refresh the JWT.",
    PORTAL_AUTH_INVALID: "Token rejected. Re-login with `lium provider portal login`.",
    PORTAL_AUTH_REFRESH_RACE: "Another lium provider process is refreshing the token; retry in a moment.",
    PORTAL_FORBIDDEN: "The portal accepted the token but refused the action for this hotkey (e.g. machine-request detail needs a validator-verified node).",
    PORTAL_CONTRACT_DRIFT: "Portal payload schema mismatch. The portal API may have changed; report to maintainers.",
    PORTAL_NOT_FOUND: "The portal returned 404 for that resource (wrong UUID or already removed).",
    PORTAL_SERVER_ERROR: "Portal 5xx. Retry; if persistent, check portal status.",
    PORTAL_RATE_LIMIT: "Backing off; retry shortly.",
    PORTAL_REQUEST_REJECTED: "The portal refused this request (see message). Fix the input; retrying the same call will not help.",
    SSH_UNREACHABLE: "SSH host unreachable. Check IP, port, firewall, and SSH key.",
    SSH_AUTH_FAILED: "SSH key/user combination rejected by the host.",
    INSTALLER_PARTIAL_FAIL: "mine.sh did not complete cleanly. Check /tmp/lium-mine.log on the host.",
    EXECUTOR_UUID_MISMATCH: "Reported executor UUID differs from the one stored in the portal.",
    UUID_NOT_FOUND: "Could not extract LIUM_EXECUTOR_UUID= marker from installer output.",
    PORTS_INVALID: "Use the form HTTP=8080,SSH=2200,RANGE=2000-2005 with positive integers.",
    ARG_INVALID: "Check the argument value and consult --help.",
    CONFIG_MISSING: "Run `lium init` or set the missing config value.",
    INPUT_REQUIRED: "Pass the value as an option; no prompt is shown without a terminal or under --json.",
    CONFIRMATION_REQUIRED: "Re-run with --yes (or set LIUM_PROVIDER_ACK=1).",
    NET_UNREACHABLE: "Nothing answered at the portal URL. Check --portal-url / LIUM_PORTAL_URL and the network, then retry.",
    HANDOFF_REQUIRED: "Relay data.message_for_human to the person, then re-run with --wait (or run it again once they are done).",
    HANDOFF_EXPIRED: "The code expired before the person finished; run the command again for a new one.",
    PORTAL_NOT_SUPPORTED: "This portal does not serve that yet; sign in with `lium provider portal login` instead.",
    API_TOKEN_NEEDS_SESSION: "An API token cannot create, list or revoke tokens: unset LIUM_PROVIDER_TOKEN and sign in (hotkey, or `lium provider portal login --email`).",
    API_TOKEN_SCOPE_MISSING: "The token lacks the scope in data.detail.required_scopes; create one with it (`lium provider token create --scope …`).",
}


# --- Error classes -------------------------------------------------------


class ProviderError(Exception):
    """Base error for the provider SDK.

    Attributes:
        code: stable string identifier (one of the constants above).
        message: short human description.
        hint: actionable next step, or empty string.
        cause: the underlying exception, if any (chained, not stringified).
        context: free-form ``dict[str, Any]``.
        legacy_code: the UPPER_CASE code a namespaced ``code`` replaces (``PORTAL_REQUEST_REJECTED`` for a
            coded 400); text mode exits by it, and ``--json`` shows it as ``legacy_code``.
    """

    default_code: str = "PROVIDER_ERROR"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        cause: BaseException | None = None,
        context: dict[str, Any] | None = None,
        legacy_code: str | None = None,
    ) -> None:
        self.code = code or self.default_code
        self.legacy_code = legacy_code
        self.message = message
        self.hint = hint if hint is not None else (_HINTS.get(self.code) or _HINTS.get(legacy_code or "", ""))
        self.cause = cause
        self.context = context or {}
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"[{self.code}] {self.message} -- {self.hint}"
        return f"[{self.code}] {self.message}"

    def to_dict(self) -> dict[str, Any]:
        """Serialisable form for ``--json`` output."""
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "context": self.context,
        }


class ProviderAuthError(ProviderError):
    default_code = PORTAL_AUTH_INVALID


class ProviderNotFoundError(ProviderError):
    default_code = PORTAL_NOT_FOUND


class ProviderServerError(ProviderError):
    default_code = PORTAL_SERVER_ERROR


class ProviderPortalContractError(ProviderError):
    """Portal returned 422; payload schema almost certainly drifted."""

    default_code = PORTAL_CONTRACT_DRIFT


class ProviderSshError(ProviderError):
    default_code = SSH_UNREACHABLE


class ProviderInstallError(ProviderError):
    default_code = INSTALLER_PARTIAL_FAIL


class ProviderConfigError(ProviderError):
    default_code = CONFIG_MISSING


__all__ = [
    "API_TOKEN_NEEDS_SESSION",
    "API_TOKEN_SCOPE_MISSING",
    "ARG_INVALID",
    "CONFIG_MISSING",
    "CONFIRMATION_REQUIRED",
    "EXECUTOR_UUID_MISMATCH",
    "EXIT_API",
    "EXIT_AUTH",
    "EXIT_BLOCKED",
    "EXIT_GENERAL",
    "EXIT_HUMAN",
    "EXIT_INTERRUPTED",
    "EXIT_INPUT",
    "EXIT_NETWORK",
    "EXIT_NOT_FOUND",
    "EXIT_NOT_LISTED",
    "EXIT_OK",
    "EXIT_RETRYABLE",
    "HANDOFF_EXPIRED",
    "HANDOFF_REQUIRED",
    "HOTKEY_NOT_REGISTERED",
    "INPUT_REQUIRED",
    "INSTALLER_PARTIAL_FAIL",
    "INTERRUPTED",
    "NET_UNREACHABLE",
    "NODE_NOT_LISTED",
    "OVERVIEW_NOT_FOR_CUSTODIED_ACCOUNT",
    "PORTAL_NOT_SUPPORTED",
    "unified_exit_code",
    "ProviderAuthError",
    "ProviderConfigError",
    "ProviderError",
    "ProviderInstallError",
    "ProviderNotFoundError",
    "ProviderPortalContractError",
    "ProviderServerError",
    "ProviderSshError",
    "PORTAL_AUTH_EXPIRED",
    "PORTAL_AUTH_INVALID",
    "PORTAL_AUTH_REFRESH_RACE",
    "PORTAL_FORBIDDEN",
    "PORTAL_CONTRACT_DRIFT",
    "PORTAL_NOT_FOUND",
    "PORTAL_RATE_LIMIT",
    "PORTAL_REQUEST_REJECTED",
    "PORTAL_SERVER_ERROR",
    "PORTS_INVALID",
    "SSH_AUTH_FAILED",
    "SSH_UNREACHABLE",
    "UUID_NOT_FOUND",
    "WALLET_NOT_FOUND",
]
