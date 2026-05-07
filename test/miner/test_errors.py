"""Tests for the miner error taxonomy."""

from __future__ import annotations

from lium.miner.errors import (
    PORTAL_AUTH_REFRESH_RACE,
    MinerAuthError,
    MinerError,
    MinerPortalContractError,
)


def test_miner_error_default_code_and_hint() -> None:
    err = MinerError("boom")
    assert err.code == "MINER_ERROR"
    assert err.message == "boom"
    # No hint registered for "MINER_ERROR" -> empty.
    assert err.hint == ""


def test_subclass_default_codes() -> None:
    assert MinerAuthError("x").code == "PORTAL_AUTH_INVALID"
    assert MinerPortalContractError("x").code == "PORTAL_CONTRACT_DRIFT"


def test_explicit_code_and_hint_override() -> None:
    err = MinerError("nope", code=PORTAL_AUTH_REFRESH_RACE)
    assert err.code == PORTAL_AUTH_REFRESH_RACE
    assert "another lium miner process" in err.hint.lower()


def test_to_dict_round_trip() -> None:
    err = MinerError(
        "command failed",
        code="ARG_INVALID",
        context={"argv": "lium miner status"},
    )
    d = err.to_dict()
    assert d["code"] == "ARG_INVALID"
    assert d["context"]["argv"] == "lium miner status"
    assert d["hint"]
