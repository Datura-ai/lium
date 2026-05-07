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
"""

from lium.provider.auth import LocalKeypairSigner, Signer
from lium.provider.client import ProviderClient
from lium.provider.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderInstallError,
    ProviderNotFoundError,
    ProviderPortalContractError,
    ProviderServerError,
    ProviderSshError,
)

__all__ = [
    "LocalKeypairSigner",
    "ProviderAuthError",
    "ProviderClient",
    "ProviderConfigError",
    "ProviderError",
    "ProviderInstallError",
    "ProviderNotFoundError",
    "ProviderPortalContractError",
    "ProviderServerError",
    "ProviderSshError",
    "Signer",
]
