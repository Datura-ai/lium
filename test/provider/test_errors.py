"""Tests for the provider error taxonomy."""

from __future__ import annotations

from lium.provider.errors import (
    PORTAL_AUTH_REFRESH_RACE,
    ProviderAuthError,
    ProviderError,
    ProviderPortalContractError,
)


def test_provider_error_default_code_and_hint() -> None:
    err = ProviderError("boom")
    assert err.code == "PROVIDER_ERROR"
    assert err.message == "boom"
    # No hint registered for "PROVIDER_ERROR" -> empty.
    assert err.hint == ""


def test_subclass_default_codes() -> None:
    assert ProviderAuthError("x").code == "PORTAL_AUTH_INVALID"
    assert ProviderPortalContractError("x").code == "PORTAL_CONTRACT_DRIFT"


def test_explicit_code_and_hint_override() -> None:
    err = ProviderError("nope", code=PORTAL_AUTH_REFRESH_RACE)
    assert err.code == PORTAL_AUTH_REFRESH_RACE
    assert "another lium provider process" in err.hint.lower()


def test_to_dict_round_trip() -> None:
    err = ProviderError(
        "command failed",
        code="ARG_INVALID",
        context={"argv": "lium provider status"},
    )
    d = err.to_dict()
    assert d["code"] == "ARG_INVALID"
    assert d["context"]["argv"] == "lium provider status"
    assert d["hint"]
