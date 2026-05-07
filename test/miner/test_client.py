"""Tests for ``lium.miner.client.MinerClient`` (M1 skeleton)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from lium.miner.auth import LocalKeypairSigner
from lium.miner.client import MinerClient
from lium.miner.errors import (
    PORTAL_AUTH_INVALID,
    MinerAuthError,
)
from lium.miner.token_store import TokenStore


class _FakePortal:
    """Tiny in-memory portal for client-level tests."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, bool]] = []
        self.gets: list[tuple[str, bool]] = []
        self.next_post: dict | None = None
        self.next_post_raises: BaseException | None = None
        self.next_get: dict | None = None

    def post(
        self,
        path: str,
        *,
        json_body: dict | None = None,
        auth: bool = True,
    ) -> dict:
        self.posts.append((path, json_body or {}, auth))
        if self.next_post_raises is not None:
            raise self.next_post_raises
        return self.next_post or {}

    def get(self, path: str, *, params: dict | None = None, auth: bool = True) -> dict:
        self.gets.append((path, auth))
        return self.next_get or {}

    # Unused stubs to satisfy structural typing if anything depends on them.
    def put(self, *a: Any, **k: Any) -> dict:  # pragma: no cover
        return {}

    def delete(self, *a: Any, **k: Any) -> dict:  # pragma: no cover
        return {}


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"fakesignature").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


def _login_response_body(token: str, hotkey: str) -> dict:
    return {
        "miner": {
            "id": "m-1",
            "miner_hotkey": hotkey,
            "miner_coldkey": "5CK",
            "created_at": "2026-05-06T00:00:00",
            "updated_at": "2026-05-06T00:00:00",
        },
        "token": token,
    }


def _build_client(
    portal: _FakePortal,
    token_store: TokenStore,
    fake_signer: LocalKeypairSigner,
) -> MinerClient:
    # PortalHTTP is the type we lie about; the in-memory fake matches the
    # methods MinerClient actually calls.
    return MinerClient(
        signer=fake_signer,
        portal_url="https://portal.example.com",
        token_store=token_store,
        http=portal,  # type: ignore[arg-type]
    )


def test_constructor_requires_signer_or_hotkey() -> None:
    with pytest.raises(ValueError):
        MinerClient()


def test_constructor_with_signer_only_works(fake_signer: LocalKeypairSigner) -> None:
    client = MinerClient(signer=fake_signer)
    assert client.hotkey_ss58 == fake_signer.ss58_address


def test_login_posts_login_flexible_with_correct_payload(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    portal.next_post = _login_response_body(
        _make_jwt(int(time.time()) + 3600), fake_signer.ss58_address
    )
    client = _build_client(portal, tmp_token_store, fake_signer)

    response = client.login()
    assert response.token == portal.next_post["token"]
    assert len(portal.posts) == 1
    path, body, auth = portal.posts[0]
    assert path == "/auth/login-flexible"
    assert auth is False  # login is unauthenticated
    assert set(body.keys()) == {"miner_hotkey", "message", "signature"}
    decoded = json.loads(body["message"])
    assert decoded["miner_hotkey"] == fake_signer.ss58_address
    # Signature is hex without the 0x prefix
    assert not body["signature"].startswith("0x")


def test_login_caches_token_and_subsequent_call_skips_network(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    portal.next_post = _login_response_body(
        _make_jwt(int(time.time()) + 3600), fake_signer.ss58_address
    )
    client = _build_client(portal, tmp_token_store, fake_signer)

    response_a = client.login()
    response_b = client.login()  # should hit the cache

    assert response_a.token == response_b.token
    assert len(portal.posts) == 1, "second login must not call portal again"


def test_login_force_re_authenticates(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    portal.next_post = _login_response_body(
        _make_jwt(int(time.time()) + 3600), fake_signer.ss58_address
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    client.login()
    portal.next_post = _login_response_body(
        _make_jwt(int(time.time()) + 7200), fake_signer.ss58_address
    )
    client.login(force=True)
    assert len(portal.posts) == 2


def test_login_invalid_response_shape_raises_auth_error(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    # Missing required `miner` field => Pydantic validation failure.
    portal.next_post = {"token": "abc"}
    client = _build_client(portal, tmp_token_store, fake_signer)
    with pytest.raises(MinerAuthError) as exc:
        client.login()
    assert exc.value.code == PORTAL_AUTH_INVALID


def test_logout_clears_cache(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    portal.next_post = _login_response_body(
        _make_jwt(int(time.time()) + 3600), fake_signer.ss58_address
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    client.login()
    assert tmp_token_store.load(fake_signer.ss58_address) is not None
    client.logout()
    assert tmp_token_store.load(fake_signer.ss58_address) is None


def test_whoami_calls_auth_me(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _FakePortal()
    portal.next_get = {"id": "m-1"}
    client = _build_client(portal, tmp_token_store, fake_signer)
    assert client.whoami() == {"id": "m-1"}
    assert portal.gets == [("/auth/me", True)]


def test_signer_lazy_default_requires_coldkey_and_hotkey() -> None:
    # No signer + no coldkey path -> error raised when signer is needed.
    client = MinerClient(hotkey="hk")
    with pytest.raises(ValueError):
        _ = client.hotkey_ss58
