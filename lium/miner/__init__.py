"""Lium miner SDK - mining-portal client + status helpers for Subnet 51.

This package adds a `lium miner ...` namespace alongside the existing renter
SDK. Hotkey registration on SN51 is performed directly with ``btcli subnet
register``; the SDK takes over from there: portal login via hotkey
signature -> SSH-install GPU executor -> add executor in portal -> observe
first non-zero validator score.

Public surface:

    from lium.miner import MinerClient
    from lium.miner.auth import Signer, LocalKeypairSigner
    from lium.miner.errors import MinerError, MinerAuthError
"""

from lium.miner.auth import LocalKeypairSigner, Signer
from lium.miner.client import MinerClient
from lium.miner.errors import (
    MinerAuthError,
    MinerConfigError,
    MinerError,
    MinerInstallError,
    MinerNotFoundError,
    MinerPortalContractError,
    MinerServerError,
    MinerSshError,
)

__all__ = [
    "LocalKeypairSigner",
    "MinerAuthError",
    "MinerClient",
    "MinerConfigError",
    "MinerError",
    "MinerInstallError",
    "MinerNotFoundError",
    "MinerPortalContractError",
    "MinerServerError",
    "MinerSshError",
    "Signer",
]
