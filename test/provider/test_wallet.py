"""Tests for ``lium.provider.wallet`` (hotkey keypair loader)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lium.provider.errors import WALLET_NOT_FOUND, ProviderError
from lium.provider.wallet import DECRYPT_FAILED_HINT, load_hotkey_keypair


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


def test_load_hotkey_keypair_encrypted_keyfile_hints_the_password() -> None:
    """An encrypted hotkey file that does not decrypt points at the password, not at creating a wallet."""

    class EncryptedWallet:
        def __init__(self, **_k: Any) -> None:
            pass

        @property
        def hotkey(self) -> Any:
            raise OSError(
                'Generic error: Failed to get hotkey: DecryptionError("Wrong password for decryption.")'
            )

    with pytest.raises(ProviderError) as exc:
        load_hotkey_keypair("default", "default", wallet_factory=EncryptedWallet)
    assert exc.value.code == WALLET_NOT_FOUND and exc.value.hint == DECRYPT_FAILED_HINT


def test_bundled_bittensor_wallet_decrypts_a_key_encrypted_by_4_1_1() -> None:
    """bittensor-wallet 4.0.1 could not decrypt a key file encrypted by 4.1.1 (btcli's current
    release), even with the right password, and lium said it could not find the wallet. The fixture is a throwaway
    test key encrypted by 4.1.1 with the password below."""
    bw = pytest.importorskip("bittensor_wallet")
    path = (
        Path(__file__).parent
        / "fixtures"
        / "hotkey_encrypted_by_bittensor_wallet_4_1_1"
    )

    keypair = bw.Keyfile(str(path)).get_keypair(password="lium-test-password")

    assert keypair.ss58_address == "5HWBZM8pGgbNwq1zwfyHAg5NRwzq5YbvRuqzNBmKJSZh3YA2"
