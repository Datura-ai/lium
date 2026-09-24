"""Lium provider SDK - mining-portal client + status helpers for Subnet 51.

This package adds a `lium provider ...` namespace alongside the existing renter
SDK. Hotkey registration on SN51 is performed directly with ``btcli subnet
register``; the SDK takes over from there: portal login via hotkey
signature -> SSH-install GPU executor -> add executor in portal -> observe
first non-zero validator score.

Public surface:

    from lium.provider import ProviderClient
    from lium.provider.auth import Signer, LocalKeypairSigner
    from lium.provider.errors import ProviderError, ProviderAuthError

The names are resolved on first use: ``lium/cli/fund/command.py`` imports
``lium.provider.chain_stack`` for its error text and ``cli.py`` imports ``fund``
at start-up, so every ``lium`` command paid for the portal client (pydantic
models, JWT) that only ``lium provider`` calls (DAH-3053).
"""

from importlib import import_module

_HOME = {
    "LocalKeypairSigner": "lium.provider.auth",
    "Signer": "lium.provider.auth",
    "ProviderClient": "lium.provider.client",
    "ProviderAuthError": "lium.provider.errors",
    "ProviderConfigError": "lium.provider.errors",
    "ProviderError": "lium.provider.errors",
    "ProviderInstallError": "lium.provider.errors",
    "ProviderNotFoundError": "lium.provider.errors",
    "ProviderPortalContractError": "lium.provider.errors",
    "ProviderServerError": "lium.provider.errors",
    "ProviderSshError": "lium.provider.errors",
}

__all__ = sorted(_HOME)


def __getattr__(name: str):
    try:
        module = _HOME[name]
    except KeyError:
        raise AttributeError(f"module 'lium.provider' has no attribute '{name}'") from None
    value = getattr(import_module(module), name)
    globals()[name] = value
    return value
