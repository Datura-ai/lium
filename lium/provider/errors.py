"""Error taxonomy for the provider SDK.

Every external failure surface (portal HTTP, SSH, wallet materialisation) is
mapped onto a stable code with an actionable hint, so an agent driving the
CLI can branch on machine-readable values rather than log strings.

Exit-code mapping (used by ``lium/cli/provider/_render.py``):

    0  success
    1  user error (bad arg)
    2  auth error
    3  portal error (server-side, not auth)
    5  SSH error
    6  config error
    7  token-cache contention (PORTAL_AUTH_REFRESH_RACE)

Each error code is exported as a string constant so callers can do::

    from lium.provider.errors import HOTKEY_NOT_REGISTERED
    if err.code == HOTKEY_NOT_REGISTERED:
        ...

PORTAL_LOGIN_REPLAY_DEBT is a *warning* code, not an error: emitted to stderr
and surfaced under ``warnings[]`` in ``lium provider status --json`` until the
portal enforces ``AUTH_MESSAGE_MAX_AGE`` on ``/auth/login-flexible``
(NEEDS-PORTAL-CHANGE filed).
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

# Portal
PORTAL_CONTRACT_DRIFT = "PORTAL_CONTRACT_DRIFT"
PORTAL_NOT_FOUND = "PORTAL_NOT_FOUND"
PORTAL_SERVER_ERROR = "PORTAL_SERVER_ERROR"
PORTAL_RATE_LIMIT = "PORTAL_RATE_LIMIT"

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

# Warnings (not raised; surfaced as info)
PORTAL_LOGIN_REPLAY_DEBT = "PORTAL_LOGIN_REPLAY_DEBT"

# Default hint table -- keep human and short. Empty string => no hint.
_HINTS: dict[str, str] = {
    WALLET_NOT_FOUND: "Run `btcli wallet new_coldkey` then `btcli wallet new_hotkey`, or check --coldkey/--hotkey names.",
    HOTKEY_NOT_REGISTERED: "Run `btcli subnet register --netuid 51 --wallet.name <coldkey> --wallet.hotkey <hotkey>` first.",
    PORTAL_AUTH_EXPIRED: "Run `lium provider portal login` to refresh the JWT.",
    PORTAL_AUTH_INVALID: "Token rejected. Re-login with `lium provider portal login`.",
    PORTAL_AUTH_REFRESH_RACE: "Another lium provider process is refreshing the token; retry in a moment.",
    PORTAL_CONTRACT_DRIFT: "Portal payload schema mismatch. The portal API may have changed; report to maintainers.",
    PORTAL_NOT_FOUND: "The portal returned 404 for that resource (wrong UUID or already removed).",
    PORTAL_SERVER_ERROR: "Portal 5xx. Retry; if persistent, check portal status.",
    PORTAL_RATE_LIMIT: "Backing off; retry shortly.",
    SSH_UNREACHABLE: "SSH host unreachable. Check IP, port, firewall, and SSH key.",
    SSH_AUTH_FAILED: "SSH key/user combination rejected by the host.",
    INSTALLER_PARTIAL_FAIL: "mine.sh did not complete cleanly. Check /tmp/lium-mine.log on the host.",
    EXECUTOR_UUID_MISMATCH: "Reported executor UUID differs from the one stored in the portal.",
    UUID_NOT_FOUND: "Could not extract LIUM_EXECUTOR_UUID= marker from installer output.",
    PORTS_INVALID: "Use the form HTTP=8080,SSH=2200,RANGE=2000-2005 with positive integers.",
    ARG_INVALID: "Check the argument value and consult --help.",
    CONFIG_MISSING: "Run `lium init` or set the missing config value.",
    PORTAL_LOGIN_REPLAY_DEBT: "Portal /auth/login-flexible does not enforce AUTH_MESSAGE_MAX_AGE; "
    "captured login bodies replay until the JWT expires. Tracked as SECURITY-DEBT.",
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
    ) -> None:
        self.code = code or self.default_code
        self.message = message
        self.hint = hint if hint is not None else _HINTS.get(self.code, "")
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
    "ARG_INVALID",
    "CONFIG_MISSING",
    "EXECUTOR_UUID_MISMATCH",
    "HOTKEY_NOT_REGISTERED",
    "INSTALLER_PARTIAL_FAIL",
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
    "PORTAL_CONTRACT_DRIFT",
    "PORTAL_LOGIN_REPLAY_DEBT",
    "PORTAL_NOT_FOUND",
    "PORTAL_RATE_LIMIT",
    "PORTAL_SERVER_ERROR",
    "PORTS_INVALID",
    "SSH_AUTH_FAILED",
    "SSH_UNREACHABLE",
    "UUID_NOT_FOUND",
    "WALLET_NOT_FOUND",
]
