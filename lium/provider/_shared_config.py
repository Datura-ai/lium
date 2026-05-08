"""Public shared-config fetcher -- default-price lookup for ``node add``.

Hits ``GET /v1/shared-config`` on lium-io-backend (``https://lium.io/api`` in
prod). The endpoint is unauthenticated and returns the same data the provider
portal frontend uses to populate its "Add Node" modal, so the CLI can match the
browser's auto-fill behaviour without the user knowing per-GPU rates.

Distinct from ``ProviderClient`` -- different host, no JWT, no token cache.
URL precedence: explicit arg > ``LIUM_SHARED_CONFIG_URL`` env > production
default.
"""

from __future__ import annotations

import os

import requests
from pydantic import BaseModel, ValidationError

from lium.provider.errors import (
    ARG_INVALID,
    PORTAL_CONTRACT_DRIFT,
    PORTAL_NOT_FOUND,
    PORTAL_SERVER_ERROR,
    ProviderError,
)

# Production lium-io-backend public endpoint. Distinct from the provider
# portal -- this is the consumer-site backend that owns ``MACHINE_PRICES``.
DEFAULT_SHARED_CONFIG_URL = "https://lium.io/api/v1/shared-config"

SHARED_CONFIG_URL_ENV = "LIUM_SHARED_CONFIG_URL"


class SharedConfigSnapshot(BaseModel):
    """Subset of fields the CLI consumes. The endpoint exposes more (collateral,
    burn emission, etc); ``model_config`` defaults allow forward-compat."""

    machine_prices: dict[str, float]
    machine_min_price_rate: float
    machine_max_price_rate: float


def fetch_shared_config(
    *,
    url: str | None = None,
    timeout: float = 10.0,
    session: requests.Session | None = None,
) -> SharedConfigSnapshot:
    target = url or os.environ.get(SHARED_CONFIG_URL_ENV) or DEFAULT_SHARED_CONFIG_URL
    sess = session or requests.Session()
    try:
        response = sess.get(target, timeout=timeout)
    except requests.RequestException as e:
        raise ProviderError(
            f"shared-config fetch failed: {e}",
            code=PORTAL_SERVER_ERROR,
            cause=e,
            context={"url": target},
        ) from e
    if response.status_code == 404:
        raise ProviderError(
            f"shared-config endpoint not found at {target}",
            code=PORTAL_NOT_FOUND,
            context={"url": target},
        )
    if not response.ok:
        raise ProviderError(
            f"shared-config returned HTTP {response.status_code}",
            code=PORTAL_SERVER_ERROR,
            context={"url": target, "status": response.status_code},
        )
    try:
        return SharedConfigSnapshot.model_validate(response.json())
    except (ValidationError, ValueError) as e:
        raise ProviderError(
            "shared-config response shape mismatch",
            code=PORTAL_CONTRACT_DRIFT,
            cause=e,
            context={"url": target},
        ) from e


def default_price_for_gpu(snapshot: SharedConfigSnapshot, gpu_type: str) -> float:
    """Return the base USD/GPU/hour for ``gpu_type``.

    Raises ``ProviderError(ARG_INVALID)`` when the GPU model isn't in the
    public price table -- the user should pass ``--price`` explicitly or
    correct the ``--gpu-type`` spelling.
    """
    if gpu_type not in snapshot.machine_prices:
        sample = ", ".join(sorted(snapshot.machine_prices)[:5])
        raise ProviderError(
            f"no default price for GPU type {gpu_type!r}; "
            f"known types include: {sample}, ... "
            f"(run `lium provider machine list` for the full set, "
            f"or pass --price explicitly)",
            code=ARG_INVALID,
            context={"gpu_type": gpu_type},
        )
    return snapshot.machine_prices[gpu_type]


__all__ = [
    "DEFAULT_SHARED_CONFIG_URL",
    "SHARED_CONFIG_URL_ENV",
    "SharedConfigSnapshot",
    "default_price_for_gpu",
    "fetch_shared_config",
]
