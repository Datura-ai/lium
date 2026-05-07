"""Hotkey-signature -> JWT exchange for the provider SDK.

This module implements:

- The ``Signer`` Protocol (A7), so v2 can drop in a HITL or remote-signer
  without touching ``ProviderClient`` call sites.
- ``LocalKeypairSigner``, the default v1 implementation that wraps
  ``bittensor.Keypair``.
- ``build_login_payload``, the *single* place that knows the wire format
  (A2: mirrors ``AuthenticationPayload(timestamp, miner_hotkey).blob_for_signing()``
  from ``lium-miner-portal/src/auth/signature_auth.py:30``).

The portal's ``verify_miner_signature`` (``lium-miner-portal/src/common/subtensor.py``)
currently accepts *any* signed string with no replay window. v1 ships
interoperable; a NEEDS-PORTAL-CHANGE ticket is filed to enforce
``AUTH_MESSAGE_MAX_AGE`` on ``/auth/login-flexible``. Until then this module
emits a one-time ``PORTAL_LOGIN_REPLAY_DEBT`` warning per process so the
SECURITY-DEBT marker is observable in stderr and structured logs.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("lium.provider.auth")

# Module-level once-flag for the SECURITY-DEBT warning. Per-process, not per
# call -- a follow-up issue tracks tuning the cadence.
_REPLAY_DEBT_WARNED = False
_REPLAY_DEBT_LOCK = threading.Lock()

REPLAY_DEBT_MESSAGE = (
    "[PORTAL_LOGIN_REPLAY_DEBT] portal /auth/login-flexible does not enforce "
    "AUTH_MESSAGE_MAX_AGE; captured login bodies replay until the JWT expires. "
    "Tracked as SECURITY-DEBT; see "
    "https://github.com/Datura-ai/lium-miner-portal (NEEDS-PORTAL-CHANGE)."
)


@runtime_checkable
class Signer(Protocol):
    """Sign an arbitrary message with a Bittensor hotkey.

    Implementations MUST return raw signature bytes (NOT hex). Hex encoding
    is the responsibility of ``build_login_payload``.

    The default implementation is :class:`LocalKeypairSigner`. Phase-2 trust
    models (HITL, hardware wallet, remote KMS) implement this Protocol and
    are injected via ``ProviderClient(signer=...)``.
    """

    @property
    def ss58_address(self) -> str:
        """The hotkey ss58 address, e.g. ``5F...``."""
        ...

    def sign(self, message: bytes) -> bytes:
        """Sign ``message`` and return raw signature bytes."""
        ...


class LocalKeypairSigner:
    """Default :class:`Signer` -- wraps a ``bittensor.Keypair`` directly.

    The keypair must be unlocked (i.e. the wallet's coldkey password has
    already been supplied to ``bittensor.Wallet`` if required). The lift of
    materialising the wallet lives in :mod:`lium.provider.wallet`.
    """

    def __init__(self, keypair: Any) -> None:
        # Avoid an import-time bittensor dependency: pass the keypair in.
        if not hasattr(keypair, "sign") or not hasattr(keypair, "ss58_address"):
            raise TypeError(
                "LocalKeypairSigner requires an object with .sign(bytes) "
                "and .ss58_address attributes (bittensor.Keypair)."
            )
        self._kp = keypair

    @property
    def ss58_address(self) -> str:
        return self._kp.ss58_address

    def sign(self, message: bytes) -> bytes:
        return self._kp.sign(message)


def _build_blob(timestamp: int, hotkey_ss58: str) -> bytes:
    """Compute ``AuthenticationPayload(...).blob_for_signing()`` exactly.

    Mirrors the canonical serialisation used throughout lium-io:

        json.dumps({"miner_hotkey": ..., "timestamp": ...}, sort_keys=True)

    Bytes are UTF-8 encoded. The function is its own canonical form so unit
    tests can assert against a known fixture.
    """
    payload: dict[str, Any] = {
        "miner_hotkey": hotkey_ss58,
        "timestamp": int(timestamp),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def build_login_payload(
    signer: Signer,
    *,
    hotkey_ss58: str | None = None,
    timestamp: int | None = None,
) -> dict[str, str]:
    """Build the ``{miner_hotkey, message, signature}`` body for login.

    Args:
        signer: any ``Signer`` -- ``LocalKeypairSigner`` by default.
        hotkey_ss58: hotkey to authenticate as. If ``None``, ``signer.ss58_address``
            is used. The two MUST agree if both supplied (defensive check).
        timestamp: explicit unix timestamp. ``None`` uses ``time.time()``.

    Returns:
        Dict with the three string fields the portal expects. ``signature``
        is hex *without* the ``0x`` prefix; the portal adds the prefix
        before calling ``verify_miner_signature`` (verified at
        ``lium-miner-portal/src/services/auth_service.py``).

    Side effect:
        Emits ``REPLAY_DEBT_MESSAGE`` once per process via ``logger.warning``.
    """
    hotkey = hotkey_ss58 or signer.ss58_address
    if hotkey_ss58 is not None and hotkey_ss58 != signer.ss58_address:
        # Defensive: the caller must not silently authenticate a different
        # hotkey than the signer can sign for.
        raise ValueError(
            f"hotkey mismatch: signer={signer.ss58_address!r} payload={hotkey_ss58!r}"
        )
    ts = int(timestamp) if timestamp is not None else int(time.time())
    blob = _build_blob(ts, hotkey)
    raw_sig = signer.sign(blob)
    if not isinstance(raw_sig, (bytes, bytearray)):
        raise TypeError(f"signer.sign must return bytes, got {type(raw_sig).__name__}")
    _emit_replay_debt_warning_once()
    return {
        "miner_hotkey": hotkey,
        "message": blob.decode("utf-8"),
        "signature": bytes(raw_sig).hex(),
    }


def _emit_replay_debt_warning_once() -> None:
    """Log the SECURITY-DEBT marker once per process.

    Idempotent and thread-safe. ``ProviderClient`` consumers can suppress the
    log by reconfiguring the ``lium.provider.auth`` logger.
    """
    global _REPLAY_DEBT_WARNED
    if _REPLAY_DEBT_WARNED:
        return
    with _REPLAY_DEBT_LOCK:
        if _REPLAY_DEBT_WARNED:
            return
        _REPLAY_DEBT_WARNED = True
    logger.warning(REPLAY_DEBT_MESSAGE)


def reset_replay_debt_warned() -> None:
    """Test helper: reset the once-flag so warning re-emits."""
    global _REPLAY_DEBT_WARNED
    with _REPLAY_DEBT_LOCK:
        _REPLAY_DEBT_WARNED = False


__all__ = [
    "REPLAY_DEBT_MESSAGE",
    "LocalKeypairSigner",
    "Signer",
    "build_login_payload",
    "reset_replay_debt_warned",
]
