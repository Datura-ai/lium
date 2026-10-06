"""Tests for ``lium.provider.auth``: Signer Protocol + login wire format."""

from __future__ import annotations

import time

import pytest

from lium.provider.auth import (
    LocalKeypairSigner,
    Signer,
    build_login_payload,
)

from .conftest import FakeKeypair


def test_local_keypair_signer_implements_protocol(fake_keypair: FakeKeypair) -> None:
    signer = LocalKeypairSigner(fake_keypair)
    # ``Signer`` is ``@runtime_checkable`` so this is a real isinstance check.
    assert isinstance(signer, Signer)
    assert signer.ss58_address == fake_keypair.ss58_address
    sig = signer.sign(b"hello")
    assert isinstance(sig, bytes) and len(sig) == 64


def test_local_keypair_signer_rejects_non_keypair() -> None:
    class NotAKeypair:
        pass

    with pytest.raises(TypeError):
        LocalKeypairSigner(NotAKeypair())


def test_build_login_payload_shape(fake_signer: LocalKeypairSigner) -> None:
    payload = build_login_payload(fake_signer, timestamp=1_700_000_000)
    assert set(payload.keys()) == {"miner_hotkey", "message", "signature"}
    assert payload["miner_hotkey"] == fake_signer.ss58_address
    # DAH-2084: message is the bare Unix timestamp string; the portal does
    # ``int(message)`` and enforces a freshness window.
    assert payload["message"] == "1700000000"
    assert int(payload["message"]) == 1_700_000_000
    # Hex sig with no 0x prefix; portal adds the prefix server-side.
    assert payload["signature"] == fake_signer.sign(payload["message"].encode()).hex()
    assert not payload["signature"].startswith("0x")


def test_build_login_payload_defaults_to_current_time(
    fake_signer: LocalKeypairSigner,
) -> None:
    before = int(time.time())
    payload = build_login_payload(fake_signer)
    after = int(time.time())
    assert before <= int(payload["message"]) <= after


def test_build_login_payload_uses_signer_address_when_hotkey_omitted(
    fake_signer: LocalKeypairSigner,
) -> None:
    payload = build_login_payload(fake_signer)
    assert payload["miner_hotkey"] == fake_signer.ss58_address


def test_build_login_payload_rejects_hotkey_mismatch(
    fake_signer: LocalKeypairSigner,
) -> None:
    with pytest.raises(ValueError, match="hotkey mismatch"):
        build_login_payload(fake_signer, hotkey_ss58="5OtherHotkey")


def test_signer_with_non_bytes_signature_rejected() -> None:
    class BadSigner:
        ss58_address = "5HK"

        def sign(self, message: bytes) -> str:  # wrong return type
            return "not-bytes"

    with pytest.raises(TypeError, match="must return bytes"):
        build_login_payload(BadSigner())  # type: ignore[arg-type]
