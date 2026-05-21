"""Test fixtures shared across the provider SDK test suite (M1)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lium.provider.auth import LocalKeypairSigner
from lium.provider.token_store import TokenStore


class FakeKeypair:
    """Minimal stand-in for ``bittensor.Keypair`` for unit tests.

    Signing is deterministic (sha256 over the message + ss58_address) so
    test assertions can pin exact wire bytes without pulling in the real
    bittensor crypto stack.
    """

    def __init__(self, ss58_address: str = "5FakeHotkey") -> None:
        self.ss58_address = ss58_address

    def sign(self, message: bytes) -> bytes:
        digest = hashlib.sha256(message + self.ss58_address.encode()).digest()
        # Bittensor sr25519 signatures are 64 bytes; mirror that to catch
        # length-related code paths.
        return digest + digest


@pytest.fixture
def fake_keypair() -> FakeKeypair:
    return FakeKeypair()


@pytest.fixture
def fake_signer(fake_keypair: FakeKeypair) -> LocalKeypairSigner:
    return LocalKeypairSigner(fake_keypair)


@pytest.fixture
def tmp_token_store(tmp_path: Path) -> TokenStore:
    """A TokenStore rooted in a tmp dir so tests don't touch ~/.lium."""
    return TokenStore(path=tmp_path / "provider-portal-token.json")


@pytest.fixture(autouse=True)
def _isolate_user_config(tmp_path_factory, monkeypatch) -> None:
    """Re-root ``~`` to a tmp dir so tests never read the developer's
    ``~/.lium/config.ini``. Without this, CLI tests that exercise the
    "missing flag" code paths spuriously pass thanks to a config-file
    fallback like ``[provider] hotkey = default``."""
    fake_home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(fake_home))
    # Pop any LIUM_PROVIDER_* env so tests start from a clean slate.
    for key in ("LIUM_PROVIDER_COLDKEY", "LIUM_PROVIDER_HOTKEY", "LIUM_PORTAL_URL"):
        monkeypatch.delenv(key, raising=False)
