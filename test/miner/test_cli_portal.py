"""CLI tests for ``lium miner portal {login,logout,whoami}`` (M2)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.miner.command import miner_command
from lium.miner.auth import LocalKeypairSigner
from lium.miner.client import MinerClient
from lium.miner.errors import MinerAuthError, MinerError
from lium.miner.token_store import TokenStore


class _Portal:
    def __init__(self, *, post_body=None, get_body=None, post_raises=None, get_raises=None):
        self._post_body = post_body
        self._get_body = get_body
        self._post_raises = post_raises
        self._get_raises = get_raises
        self.posts: list[Any] = []
        self.gets: list[Any] = []

    def post(self, path, *, json_body=None, auth=True):
        self.posts.append((path, json_body, auth))
        if self._post_raises:
            raise self._post_raises
        return self._post_body or {}

    def get(self, path, *, params=None, auth=True):
        self.gets.append((path, auth))
        if self._get_raises:
            raise self._get_raises
        return self._get_body or {}

    def put(self, *a, **k):  # pragma: no cover
        return {}

    def delete(self, *a, **k):  # pragma: no cover
        return {}


def _make_jwt(exp: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
        .rstrip(b"=")
        .decode()
    )
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{header}.{payload}.{sig}"


@pytest.fixture
def patched_build_client(monkeypatch, fake_signer: LocalKeypairSigner, tmp_token_store: TokenStore):
    """Replace ``build_client`` so CLI tests inject a fake portal + signer."""
    portals: list[_Portal] = []

    def _factory(portal: _Portal):
        portals.append(portal)

        def _builder(ctx):
            client = MinerClient(
                signer=fake_signer,
                token_store=tmp_token_store,
                http=portal,  # type: ignore[arg-type]
            )
            return client

        for module in (
            "lium.cli.miner.portal",
            "lium.cli.miner.status",
        ):
            monkeypatch.setattr(f"{module}.build_client", _builder)

    return _factory


def test_portal_login_emits_summary(patched_build_client, fake_signer) -> None:
    portal = _Portal(
        post_body={
            "miner": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "miner_coldkey": "5CK",
                "created_at": "2026-05-06T00:00:00",
                "updated_at": "2026-05-06T00:00:00",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        }
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    assert "miner_id=m-7" in result.output
    assert portal.posts and portal.posts[0][0] == "/auth/login-flexible"


def test_portal_login_json_envelope(patched_build_client, fake_signer) -> None:
    portal = _Portal(
        post_body={
            "miner": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "miner_coldkey": "5CK",
                "created_at": "x",
                "updated_at": "x",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        }
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "--json", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["miner_id"] == "m-7"


def test_portal_login_auth_error_returns_exit_2(patched_build_client) -> None:
    portal = _Portal(post_raises=MinerAuthError("bad signature"))
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 2, result.output


def test_portal_whoami_renders_body(patched_build_client) -> None:
    portal = _Portal(get_body={"id": "m-1", "miner_hotkey": "5xxx"})
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "portal", "whoami"],
    )
    assert result.exit_code == 0, result.output
    assert "portal session active" in result.output
    assert portal.gets == [("/auth/me", True)]


def test_portal_logout_clears_cache(
    patched_build_client, fake_signer, tmp_token_store: TokenStore
) -> None:
    portal = _Portal()
    patched_build_client(portal)
    # Pre-populate the cache.
    tmp_token_store.save(
        fake_signer.ss58_address,
        _make_jwt(int(time.time()) + 3600),
        miner_id="m-1",
    )
    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "portal", "logout"],
    )
    assert result.exit_code == 0, result.output
    assert tmp_token_store.load(fake_signer.ss58_address) is None


def test_portal_login_requires_hotkey(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(miner_command, ["portal", "login"])
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output


def test_portal_generic_miner_error_returns_exit_3(patched_build_client) -> None:
    portal = _Portal(post_raises=MinerError("server boom", code="PORTAL_SERVER_ERROR"))
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        miner_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 3, result.output
