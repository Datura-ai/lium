"""Tests for ``lium.miner.wallet`` (hotkey keypair loader)."""

from __future__ import annotations

from typing import Any

import pytest

from lium.miner.errors import WALLET_NOT_FOUND, MinerError
from lium.miner.wallet import load_hotkey_keypair


def test_load_hotkey_keypair_with_factory() -> None:
    class FakeKeypair:
        ss58_address = "5HK"

        def sign(self, message: bytes) -> bytes:
            return b"\x00" * 64

    class FakeWallet:
        def __init__(self, name: str, hotkey: str) -> None:
            self.hotkey = FakeKeypair()

    keypair = load_hotkey_keypair("default", "hk", wallet_factory=FakeWallet)
    assert keypair.ss58_address == "5HK"


def test_load_hotkey_keypair_factory_failure_wrapped() -> None:
    def boom(**_kwargs: Any) -> None:
        raise RuntimeError("disk gone")

    with pytest.raises(MinerError) as exc:
        load_hotkey_keypair("default", "hk", wallet_factory=boom)
    assert exc.value.code == WALLET_NOT_FOUND


def test_load_hotkey_keypair_no_signable_hotkey() -> None:
    class WalletWithoutHotkey:
        def __init__(self, **_k: Any) -> None:
            self.hotkey = object()  # no .sign attribute

    with pytest.raises(MinerError) as exc:
        load_hotkey_keypair("default", "hk", wallet_factory=WalletWithoutHotkey)
    assert exc.value.code == WALLET_NOT_FOUND
