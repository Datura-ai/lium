"""Wallet / keypair helpers for the provider SDK.

Thin wrapper around ``bittensor.Wallet`` so unit tests can stub the
materialisation step without pulling 120MB of bittensor deps into the
test runtime. ``ProviderClient`` consumers can also bypass this module
entirely by injecting a custom :class:`lium.provider.auth.Signer`.

This module deals only with the *hotkey* keypair, which is unencrypted in
a default bittensor install -- no password handling lives here. Coldkey
operations (e.g. registering on a subnet) are delegated to ``btcli`` run
by the user directly.
"""

from __future__ import annotations

from typing import Any

from lium.provider.chain_stack import missing_chain_stack_message
from lium.provider.errors import (
    WALLET_NOT_FOUND,
    ProviderConfigError,
    ProviderError,
)


DECRYPT_FAILED_HINT = (
    "The hotkey file is password-encrypted and did not decrypt: check the password. A file encrypted by a newer "
    "bittensor-wallet than this lium build bundles fails the same way."
)


def load_hotkey_keypair(
    coldkey: str,
    hotkey: str,
    *,
    wallet_factory: Any | None = None,
) -> Any:
    """Materialise a hotkey ``Keypair`` from disk (``~/.bittensor/wallets/``).

    Args:
        coldkey: coldkey name.
        hotkey: hotkey name.
        wallet_factory: optional callable returning a ``bittensor.Wallet``-like
            object; tests inject a fake. If ``None`` we import ``bittensor``
            lazily.

    Returns:
        The hotkey keypair (an object with ``.sign(bytes) -> bytes`` and
        ``.ss58_address`` attributes).
    """
    if wallet_factory is None:
        try:
            import bittensor  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover - dep missing
            raise ProviderConfigError(
                f"loading a wallet needs the chain stack. Reason: {missing_chain_stack_message()}",
                hint="",  # the reason names the fix; `lium init` is the renter's setup
                cause=e,
            ) from e
        wallet_factory = bittensor.Wallet
    try:
        wallet = wallet_factory(name=coldkey, hotkey=hotkey)
        # bittensor.Wallet defers keyfile reads to attribute access, so the
        # FileNotFound surfaces here, not in the constructor above.
        keypair = wallet.hotkey
    except Exception as e:
        # the cause goes into the message: it names the keyfile read and why it failed
        # (missing, unreadable, encrypted), which is what differs when btcli lists the wallet
        raise ProviderError(
            f"could not open wallet name={coldkey!r} hotkey={hotkey!r}: {type(e).__name__}: {e}",
            code=WALLET_NOT_FOUND,
            # the keyfile is there but password-encrypted: "create a wallet" is the wrong next step. The bundled
            # bittensor-wallet raises this both for a wrong password and for a file encrypted by a newer release
            hint=DECRYPT_FAILED_HINT if "DecryptionError" in str(e) else None,
            cause=e,
            context={"coldkey": coldkey, "hotkey": hotkey},
        ) from e
    if keypair is None or not hasattr(keypair, "sign"):
        raise ProviderError(
            f"wallet name={coldkey!r} hotkey={hotkey!r} has no usable hotkey keypair",
            code=WALLET_NOT_FOUND,
            context={"coldkey": coldkey, "hotkey": hotkey},
        )
    return keypair


__all__ = [
    "load_hotkey_keypair",
]
