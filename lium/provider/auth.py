"""Hotkey-signature -> JWT exchange for the provider SDK.

This module implements:

- The ``Signer`` Protocol (A7), so v2 can drop in a HITL or remote-signer
  without touching ``ProviderClient`` call sites.
- ``LocalKeypairSigner``, the default v1 implementation that wraps
  ``bittensor.Keypair``.
- ``build_login_payload``, the *single* place that knows the wire format.

Wire format (DAH-2084)
----------------------
The portal's ``/auth/login-flexible`` endpoint authenticates the *signed
message itself* as a freshness proof: the ``message`` field MUST be the
current Unix timestamp as a decimal string, and the portal
(``AuthService._verify_login_signature``) does ``int(message)`` and rejects
anything outside ``LOGIN_SIGNATURE_MAX_AGE``, also burning a Redis nonce so a
captured body cannot be replayed.

Earlier revisions of this module signed a JSON blob
(``{"miner_hotkey": ..., "timestamp": ...}``); that no longer parses as an
int and is rejected by the hardened portal. ``build_login_payload`` now signs
the bare timestamp string so the SDK stays interoperable with the fix.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable


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
            Callers should leave this ``None`` outside of tests -- the portal
            rejects stale timestamps (DAH-2084).

    Returns:
        Dict with the three string fields the portal expects. ``message`` is
        the current Unix timestamp as a decimal string -- the portal does
        ``int(message)`` and enforces a freshness window. ``signature`` is hex
        *without* the ``0x`` prefix; the portal adds the prefix before calling
        ``verify_miner_signature``.
    """
    hotkey = hotkey_ss58 or signer.ss58_address
    if hotkey_ss58 is not None and hotkey_ss58 != signer.ss58_address:
        # Defensive: the caller must not silently authenticate a different
        # hotkey than the signer can sign for.
        raise ValueError(
            f"hotkey mismatch: signer={signer.ss58_address!r} payload={hotkey_ss58!r}"
        )
    ts = int(timestamp) if timestamp is not None else int(time.time())
    message = str(ts)
    raw_sig = signer.sign(message.encode("utf-8"))
    if not isinstance(raw_sig, (bytes, bytearray)):
        raise TypeError(f"signer.sign must return bytes, got {type(raw_sig).__name__}")
    return {
        "miner_hotkey": hotkey,
        "message": message,
        "signature": bytes(raw_sig).hex(),
    }


__all__ = [
    "LocalKeypairSigner",
    "Signer",
    "build_login_payload",
]
