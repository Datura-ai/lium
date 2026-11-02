"""Provider portal sign-in with an authenticator (TOTP) step: a security guard kept through the DAH-4027 cut (DAH-4746)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest

from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import (
    ProviderAuthError,
)
from lium.provider.token_store import TokenStore


class _FakePortal:
    """Tiny in-memory portal for client-level tests."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict, bool]] = []
        self.gets: list[tuple[str, bool]] = []
        self.next_post: dict | None = None
        self.next_post_raises: BaseException | None = None
        self.next_get: dict | None = None
        self.next_get_raises: BaseException | None = None

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
        if self.next_get_raises is not None:
            raise self.next_get_raises
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
        "provider": {
            "id": "m-1",
            "miner_hotkey": hotkey,
            "provider_coldkey": "5CK",
            "created_at": "2026-05-06T00:00:00",
            "updated_at": "2026-05-06T00:00:00",
        },
        "token": token,
    }


def _build_client(
    portal: _FakePortal,
    token_store: TokenStore,
    fake_signer: LocalKeypairSigner,
) -> ProviderClient:
    # PortalHTTP is the type we lie about; the in-memory fake matches the
    # methods ProviderClient actually calls.
    return ProviderClient(
        signer=fake_signer,
        portal_url="https://portal.example.com",
        token_store=token_store,
        http=portal,  # type: ignore[arg-type]
    )


class _SequencedPortal(_FakePortal):
    """Answers each POST with the next body in ``answers``."""

    def __init__(self, answers: list[dict]) -> None:
        super().__init__()
        self.answers = answers

    def post(self, path: str, *, json_body: dict | None = None, auth: bool = True) -> dict:
        self.posts.append((path, json_body or {}, auth))
        return self.answers.pop(0)


def _mfa_challenge_body(hotkey: str) -> dict:
    body = _login_response_body("", hotkey)
    body.update(token=None, mfa_required=True, mfa_token="challenge-1")
    return body


def test_login_sends_the_authenticator_code_when_the_portal_asks_for_it(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    token = _make_jwt(int(time.time()) + 3600)
    portal = _SequencedPortal(
        [_mfa_challenge_body(fake_signer.ss58_address), _login_response_body(token, fake_signer.ss58_address)]
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    client.totp_code = lambda: " 123456 "

    response = client.login()

    assert response.token == token
    assert portal.posts[1] == ("/auth/login-totp", {"mfa_token": "challenge-1", "code": "123456"}, False)
    assert tmp_token_store.load(fake_signer.ss58_address).token == token


def test_login_without_a_code_source_refuses_an_authenticator_account(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    portal = _SequencedPortal([_mfa_challenge_body(fake_signer.ss58_address)])
    client = _build_client(portal, tmp_token_store, fake_signer)

    with pytest.raises(ProviderAuthError) as excinfo:
        client.login()

    assert excinfo.value.code == "PORTAL_AUTH_TOTP_REQUIRED"
    assert [path for path, _, _ in portal.posts] == ["/auth/login-flexible"]
    assert tmp_token_store.load(fake_signer.ss58_address) is None


def test_a_wrong_authenticator_code_is_its_own_error_not_a_forbidden_action(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    class _WrongCodePortal(_SequencedPortal):
        def post(self, path: str, *, json_body: dict | None = None, auth: bool = True) -> dict:
            if path == "/auth/login-totp":
                self.posts.append((path, json_body or {}, auth))
                raise ProviderAuthError("portal forbade the requested action", code="PORTAL_FORBIDDEN")
            return super().post(path, json_body=json_body, auth=auth)

    portal = _WrongCodePortal([_mfa_challenge_body(fake_signer.ss58_address)])
    client = _build_client(portal, tmp_token_store, fake_signer)
    client.totp_code = lambda: "000000"

    with pytest.raises(ProviderAuthError) as excinfo:
        client.login()

    assert excinfo.value.code == "PORTAL_AUTH_TOTP_INVALID"
    assert tmp_token_store.load(fake_signer.ss58_address) is None


def test_email_sign_in_sends_the_authenticator_code_too(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    """The portal asks every sign-in route for the code (DAH-4455), /auth/login-email included."""
    token = _make_jwt(int(time.time()) + 3600)
    portal = _SequencedPortal([_mfa_challenge_body("acct-1"), _login_response_body(token, "acct-1")])
    client = ProviderClient(
        portal_url="https://portal.example.com",
        account="me@example.com",
        token_store=tmp_token_store,
        http=portal,  # type: ignore[arg-type]
    )
    client.totp_code = lambda: "123456"

    response = client.login_email("me@example.com", "pw")

    assert response.token == token
    assert portal.posts[1] == ("/auth/login-totp", {"mfa_token": "challenge-1", "code": "123456"}, False)
    assert tmp_token_store.load("acct-1").token == token


def test_set_email_asks_for_a_second_code(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    """The portal spends one code on the sign-in and asks for another one for the address itself (DAH-4456)."""
    token = _make_jwt(int(time.time()) + 3600)
    portal = _SequencedPortal(
        [
            _mfa_challenge_body(fake_signer.ss58_address),
            _login_response_body(token, fake_signer.ss58_address),
            {"email": "new@example.com"},
        ]
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    codes = iter(["111111", "222222"])
    client.totp_code = lambda: next(codes)

    client.set_email("new@example.com")

    assert portal.posts[1][1]["code"] == "111111"
    assert portal.posts[2] == ("/auth/set-email", {"email": "new@example.com", "totp_code": "222222"}, True)


def test_set_email_of_an_account_without_an_authenticator_asks_nothing(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Today every account is this one: a terminal must not be asked for a code it does not have."""
    from lium.cli.provider import _client

    token = _make_jwt(int(time.time()) + 3600)
    portal = _SequencedPortal(
        [_login_response_body(token, fake_signer.ss58_address), {"email": "new@example.com"}]
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    monkeypatch.setattr(_client, "is_interactive", lambda: True)
    asked: list[str] = []
    monkeypatch.setattr(_client.click, "prompt", lambda text, **kw: asked.append(text) or "999999")
    _client.attach_totp_code(client, None)

    client.set_email("new@example.com")

    assert asked == []
    assert portal.posts[-1][1] == {"email": "new@example.com", "totp_code": None}


def test_the_given_code_answers_one_ask_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--totp-code` is one code: a command that needs a second one asks, and a script is refused."""
    from lium.cli.provider import _client
    from lium.provider.client import ProviderClient as _Client

    monkeypatch.setattr(_client, "is_interactive", lambda: False)
    client = _client.attach_totp_code(
        _Client(portal_url="https://portal.example.com", account="me@example.com"), "111111"
    )

    assert client.totp_code() == "111111"
    with pytest.raises(ProviderAuthError) as excinfo:
        client.totp_code()
    assert excinfo.value.code == "PORTAL_AUTH_TOTP_REQUIRED"


def test_a_wrong_code_on_the_new_address_reads_as_a_wrong_code(
    fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore
) -> None:
    """The portal refuses it with the same 403 it uses for a forbidden action; the CLI must not say that."""
    token = _make_jwt(int(time.time()) + 3600)

    class _RefusingPortal(_SequencedPortal):
        def post(self, path: str, *, json_body: dict | None = None, auth: bool = True) -> dict:
            if path == "/auth/set-email":
                raise ProviderAuthError("portal forbade the requested action", code="PORTAL_FORBIDDEN")
            return super().post(path, json_body=json_body, auth=auth)

    portal = _RefusingPortal(
        [_mfa_challenge_body(fake_signer.ss58_address), _login_response_body(token, fake_signer.ss58_address)]
    )
    client = _build_client(portal, tmp_token_store, fake_signer)
    codes = iter(["111111", "222222"])
    client.totp_code = lambda: next(codes)

    with pytest.raises(ProviderAuthError) as excinfo:
        client.set_email("new@example.com")

    assert excinfo.value.code == "PORTAL_AUTH_TOTP_INVALID"
