"""Tests for ``lium.provider._shared_config`` -- public default-price lookup."""

from __future__ import annotations

import pytest
import requests

from lium.provider._shared_config import (
    DEFAULT_SHARED_CONFIG_URL,
    SHARED_CONFIG_URL_ENV,
    SharedConfigSnapshot,
    default_price_for_gpu,
    fetch_shared_config,
)
from lium.provider.errors import (
    ARG_INVALID,
    PORTAL_CONTRACT_DRIFT,
    PORTAL_NOT_FOUND,
    PORTAL_SERVER_ERROR,
    ProviderError,
)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None) -> None:
        self.status_code = status_code
        self.ok = 200 <= status_code < 400
        self._body = body or {}

    def json(self) -> dict:
        return self._body


class _FakeSession:
    """Records ``get()`` calls and returns a queued response or raises."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.calls: list[tuple[str, float | None]] = []

    def get(self, url: str, *, timeout: float | None = None) -> _FakeResponse:
        self.calls.append((url, timeout))
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


_VALID_BODY = {
    "machine_prices": {"NVIDIA H100 80GB HBM3": 1.26, "NVIDIA H200": 1.9},
    "machine_min_price_rate": 0.5,
    "machine_max_price_rate": 3.0,
    # Extra fields the endpoint returns -- forward-compat: must be ignored.
    "rental_fees_rate": 0.1,
    "collateral_days": 14,
}


def test_fetch_shared_config_parses_subset() -> None:
    session = _FakeSession(_FakeResponse(200, _VALID_BODY))
    snap = fetch_shared_config(url="https://example.test/cfg", session=session)
    assert snap.machine_prices["NVIDIA H200"] == 1.9
    assert snap.machine_min_price_rate == 0.5
    assert snap.machine_max_price_rate == 3.0
    assert session.calls[0][0] == "https://example.test/cfg"


def test_fetch_shared_config_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv(SHARED_CONFIG_URL_ENV, "https://override.test/cfg")
    session = _FakeSession(_FakeResponse(200, _VALID_BODY))
    fetch_shared_config(session=session)
    assert session.calls[0][0] == "https://override.test/cfg"


def test_fetch_shared_config_default_url_when_no_env(monkeypatch) -> None:
    monkeypatch.delenv(SHARED_CONFIG_URL_ENV, raising=False)
    session = _FakeSession(_FakeResponse(200, _VALID_BODY))
    fetch_shared_config(session=session)
    assert session.calls[0][0] == DEFAULT_SHARED_CONFIG_URL


def test_fetch_shared_config_404_maps_to_not_found() -> None:
    session = _FakeSession(_FakeResponse(404))
    with pytest.raises(ProviderError) as exc_info:
        fetch_shared_config(url="https://example.test/cfg", session=session)
    assert exc_info.value.code == PORTAL_NOT_FOUND


def test_fetch_shared_config_5xx_maps_to_server_error() -> None:
    session = _FakeSession(_FakeResponse(503))
    with pytest.raises(ProviderError) as exc_info:
        fetch_shared_config(url="https://example.test/cfg", session=session)
    assert exc_info.value.code == PORTAL_SERVER_ERROR


def test_fetch_shared_config_network_error_maps_to_server_error() -> None:
    session = _FakeSession(requests.ConnectionError("boom"))
    with pytest.raises(ProviderError) as exc_info:
        fetch_shared_config(url="https://example.test/cfg", session=session)
    assert exc_info.value.code == PORTAL_SERVER_ERROR
    assert "boom" in str(exc_info.value)


def test_fetch_shared_config_bad_shape_maps_to_contract_drift() -> None:
    # Missing required ``machine_prices`` field
    session = _FakeSession(_FakeResponse(200, {"machine_min_price_rate": 0.5}))
    with pytest.raises(ProviderError) as exc_info:
        fetch_shared_config(url="https://example.test/cfg", session=session)
    assert exc_info.value.code == PORTAL_CONTRACT_DRIFT


def test_default_price_for_gpu_returns_value() -> None:
    snap = SharedConfigSnapshot.model_validate(_VALID_BODY)
    assert default_price_for_gpu(snap, "NVIDIA H200") == 1.9


def test_default_price_for_gpu_unknown_raises_arg_invalid() -> None:
    snap = SharedConfigSnapshot.model_validate(_VALID_BODY)
    with pytest.raises(ProviderError) as exc_info:
        default_price_for_gpu(snap, "NVIDIA NONEXISTENT")
    assert exc_info.value.code == ARG_INVALID
    assert "NONEXISTENT" in str(exc_info.value)
