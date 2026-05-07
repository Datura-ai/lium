"""Tests for ``lium.miner.token_store`` (A5 atomic + flock)."""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path

import pytest

from lium.miner.errors import PORTAL_AUTH_REFRESH_RACE, MinerError
from lium.miner.token_store import (
    CachedToken,
    TokenStore,
    _decode_jwt_exp,
    with_refresh_retry,
)


def _make_jwt(exp: int) -> str:
    """Build an unsigned JWT with the given exp claim. Signature is a valid
    b64url placeholder so PyJWT's structural decoder accepts it."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"fakesignature").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def test_save_and_load_round_trip(tmp_token_store: TokenStore) -> None:
    exp = int(time.time()) + 3600
    token = _make_jwt(exp)
    cached = tmp_token_store.save("5HK", token, miner_id="miner-1")
    assert cached.token == token
    assert cached.miner_id == "miner-1"
    assert cached.exp == exp
    assert not cached.expired()

    reloaded = tmp_token_store.load("5HK")
    assert reloaded is not None
    assert reloaded.token == token
    assert reloaded.miner_id == "miner-1"


def test_load_missing_returns_none(tmp_token_store: TokenStore) -> None:
    assert tmp_token_store.load("5HK") is None


def test_load_expired_returns_none(tmp_token_store: TokenStore) -> None:
    token = _make_jwt(int(time.time()) - 60)
    tmp_token_store.save("5HK", token)
    assert tmp_token_store.load("5HK") is None


def test_clear_specific_hotkey(tmp_token_store: TokenStore) -> None:
    exp = int(time.time()) + 3600
    tmp_token_store.save("5HK_A", _make_jwt(exp), miner_id="a")
    tmp_token_store.save("5HK_B", _make_jwt(exp), miner_id="b")
    tmp_token_store.clear("5HK_A")
    assert tmp_token_store.load("5HK_A") is None
    assert tmp_token_store.load("5HK_B") is not None


def test_clear_all(tmp_token_store: TokenStore) -> None:
    exp = int(time.time()) + 3600
    tmp_token_store.save("5HK_A", _make_jwt(exp))
    tmp_token_store.save("5HK_B", _make_jwt(exp))
    tmp_token_store.clear()
    assert tmp_token_store.load("5HK_A") is None
    assert tmp_token_store.load("5HK_B") is None


def test_save_creates_file_with_0600_perms(tmp_token_store: TokenStore) -> None:
    tmp_token_store.save("5HK", _make_jwt(int(time.time()) + 3600))
    mode = os.stat(tmp_token_store.path).st_mode & 0o777
    assert mode == 0o600


def test_save_atomic_write_no_partial_file(
    tmp_token_store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If json.dump fails, the on-disk file should not be left half-written."""
    # First, populate a valid file.
    tmp_token_store.save("5HK", _make_jwt(int(time.time()) + 3600), miner_id="orig")

    real_replace = os.replace

    def boom_replace(*_a: object, **_k: object) -> None:
        raise OSError("disk full simulation")

    monkeypatch.setattr(os, "replace", boom_replace)
    with pytest.raises(OSError):
        tmp_token_store.save("5HK", _make_jwt(int(time.time()) + 3600), miner_id="new")
    monkeypatch.setattr(os, "replace", real_replace)

    # Original entry must still be intact.
    reloaded = tmp_token_store.load("5HK")
    assert reloaded is not None and reloaded.miner_id == "orig"

    # No leftover .tmp file lingering.
    tmp_files = list(Path(tmp_token_store.path.parent).glob("*.tmp"))
    assert tmp_files == []


def test_flock_contention_raises_refresh_race(tmp_path: Path) -> None:
    """A second TokenStore that holds the lock blocks the first."""
    pytest.importorskip("fcntl")
    import fcntl as _fcntl

    store_path = tmp_path / "miner-portal-token.json"
    lock_path = store_path.with_suffix(store_path.suffix + ".lock")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    holder_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _fcntl.flock(holder_fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
        store = TokenStore(path=store_path)
        with pytest.raises(MinerError) as exc:
            store.save("5HK", _make_jwt(int(time.time()) + 3600))
        assert exc.value.code == PORTAL_AUTH_REFRESH_RACE
    finally:
        try:
            _fcntl.flock(holder_fd, _fcntl.LOCK_UN)
        finally:
            os.close(holder_fd)


def test_with_refresh_retry_recovers(tmp_token_store: TokenStore) -> None:
    calls = {"n": 0}

    def maybe_succeeds() -> str:
        calls["n"] += 1
        if calls["n"] < 2:
            raise MinerError("locked", code=PORTAL_AUTH_REFRESH_RACE)
        return "ok"

    result = with_refresh_retry(maybe_succeeds, max_retries=3, delay_range=(0.0, 0.0))
    assert result == "ok"
    assert calls["n"] == 2


def test_with_refresh_retry_propagates_other_errors() -> None:
    def boom() -> None:
        raise MinerError("nope", code="OTHER_CODE")

    with pytest.raises(MinerError):
        with_refresh_retry(boom, max_retries=3, delay_range=(0.0, 0.0))


def test_decode_jwt_exp_with_valid_token() -> None:
    exp = int(time.time()) + 7200
    assert _decode_jwt_exp(_make_jwt(exp)) == exp


def test_decode_jwt_exp_with_garbage_falls_back_to_one_week() -> None:
    now = int(time.time())
    fallback = _decode_jwt_exp("not.a.jwt")
    assert now + (6 * 24 * 3600) < fallback <= now + (8 * 24 * 3600)


def test_cached_token_expired_with_leeway() -> None:
    now = int(time.time())
    almost = CachedToken(token="t", exp=now + 10)
    # leeway 30 seconds means a token expiring in 10s is reported expired.
    assert almost.expired(leeway_seconds=30, now=now) is True
    assert almost.expired(leeway_seconds=0, now=now) is False
