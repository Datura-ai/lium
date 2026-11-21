"""Tests for ``lium.provider.wallet`` (hotkey keypair loader)."""

from __future__ import annotations

from typing import Any

import pytest

from lium.provider.errors import WALLET_NOT_FOUND, ProviderError
from lium.provider.wallet import load_hotkey_keypair


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

    with pytest.raises(ProviderError) as exc:
        load_hotkey_keypair("default", "hk", wallet_factory=boom)
    assert exc.value.code == WALLET_NOT_FOUND
    # the underlying error is shown, not only kept as the chained cause (the CLI printed none of it)
    assert (
        "RuntimeError: disk gone" in exc.value.message
        and "RuntimeError: disk gone" in str(exc.value)
    )


def test_load_hotkey_keypair_names_the_missing_keyfile() -> None:
    """A wallet whose private hotkey file is absent (its public file alone still lists it) says which path it read."""
    path = "/home/u/.bittensor/wallets/default/hotkeys/default"

    class LazyWallet:
        def __init__(self, **_k: Any) -> None:
            pass

        @property
        def hotkey(self) -> Any:
            raise OSError(
                f'Failed to get hotkey: FileNotFound("Keyfile at: {path} does not exist.")'
            )

    with pytest.raises(ProviderError) as exc:
        load_hotkey_keypair("default", "default", wallet_factory=LazyWallet)
    assert exc.value.code == WALLET_NOT_FOUND and path in str(exc.value)


def test_load_hotkey_keypair_no_signable_hotkey() -> None:
    class WalletWithoutHotkey:
        def __init__(self, **_k: Any) -> None:
            self.hotkey = object()  # no .sign attribute

    with pytest.raises(ProviderError) as exc:
        load_hotkey_keypair("default", "hk", wallet_factory=WalletWithoutHotkey)
    assert exc.value.code == WALLET_NOT_FOUND
