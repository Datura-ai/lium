"""Tests for ``lium.provider.wallet`` (hotkey keypair loader)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lium.provider.errors import WALLET_NOT_FOUND, WALLET_PASSWORD_WRONG, ProviderError
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


class WrongPasswordError(ValueError):
    """Stands in for bittensor 11.3+'s ``bittensor.sp_core.WrongPasswordError``."""


@pytest.mark.parametrize(
    "wrong_password_error",
    [
        # bittensor-wallet 4.x (bittensor 9/10): one KeyFileError type, the variant named in its text
        OSError('Failed to get hotkey: DecryptionError("Wrong password for decryption.")'),
        # bittensor 11.3+: its own exception type
        WrongPasswordError("wrong password for NaCl decryption"),
    ],
)
def test_load_hotkey_keypair_wrong_password_is_not_a_missing_wallet(wrong_password_error: Exception) -> None:
    """An encrypted hotkey file and a wrong password: the wallet exists, so the code and hint name the password,
    not creating a wallet."""

    class EncryptedWallet:
        def __init__(self, **_k: Any) -> None:
            pass

        @property
        def hotkey(self) -> Any:
            raise wrong_password_error

    with pytest.raises(ProviderError) as exc:
        load_hotkey_keypair("pwwallet", "enchot", wallet_factory=EncryptedWallet)
    assert exc.value.code == WALLET_PASSWORD_WRONG
    assert "password" in exc.value.hint and "new_coldkey" not in exc.value.hint


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


def test_bundled_bittensor_wallet_reads_an_unencrypted_key_file_in_the_4_1_format(
    tmp_path: Path,
) -> None:
    """bittensor-wallet 4.1 writes unencrypted key files with a ``cryptoType`` field, which 4.0.1 refuses to read: lium
    reported such a wallet as not found. The key is the public dev account //Alice."""
    bw = pytest.importorskip("bittensor_wallet")
    alice = bw.Keypair.create_from_uri("//Alice")
    path = tmp_path / "default"
    bw.Keyfile(str(path)).set_keypair(alice, encrypt=False, overwrite=True)
    data = json.loads(path.read_text())
    data["cryptoType"] = 1  # the 4.1 format's sr25519 marker
    path.write_text(json.dumps(data))

    keypair = bw.Keyfile(str(path)).get_keypair()

    assert keypair.ss58_address == alice.ss58_address
