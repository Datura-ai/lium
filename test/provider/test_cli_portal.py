"""CLI tests for ``lium provider portal {login,logout,whoami}`` (M2)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from lium.provider.auth import LocalKeypairSigner
from lium.provider.client import ProviderClient
from lium.provider.errors import ProviderAuthError, ProviderError
from lium.provider.token_store import TokenStore


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
            client = ProviderClient(
                signer=fake_signer,
                token_store=tmp_token_store,
                http=portal,  # type: ignore[arg-type]
            )
            return client

        for module in (
            "lium.cli.provider.portal",
            "lium.cli.provider.status",
        ):
            monkeypatch.setattr(f"{module}.build_client", _builder)

    return _factory


def test_portal_login_emits_summary(patched_build_client, fake_signer) -> None:
    portal = _Portal(
        post_body={
            "provider": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "provider_coldkey": "5CK",
                "created_at": "2026-05-06T00:00:00",
                "updated_at": "2026-05-06T00:00:00",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        }
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    assert "provider_id=m-7" in result.output
    assert portal.posts and portal.posts[0][0] == "/auth/login-flexible"


def test_portal_login_json_envelope(patched_build_client, fake_signer) -> None:
    portal = _Portal(
        post_body={
            "provider": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "provider_coldkey": "5CK",
                "discord_id": "5477543105",
                "created_at": "x",
                "updated_at": "x",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        },
        get_body={"discord_id": "5477543105"},
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "--json", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["ok"] is True
    assert payload["data"]["provider_id"] == "m-7"
    assert payload["data"]["discord_connected"] is True
    assert payload["data"]["extra_incentive_eligible"] is True


def test_portal_login_warns_when_discord_missing(
    patched_build_client, fake_signer
) -> None:
    portal = _Portal(
        post_body={
            "provider": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "provider_coldkey": "5CK",
                "discord_id": None,
                "created_at": "x",
                "updated_at": "x",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        },
        get_body={"discord_id": None},
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    assert "Token Present" in result.output
    assert "Discord Connected" in result.output
    assert "Extra Incentives" in result.output
    assert (
        result.output.index("Token Present")
        < result.output.index("Discord Connected")
        < result.output.index("Extra Incentives")
    )
    assert "No Discord = no extra incentives" in result.output
    assert "lium provider config connect-discord" in result.output
    assert "Extra Incentive Eligible" not in result.output
    assert "DISCORD_REQUIRED_FOR_EXTRA_INCENTIVES" not in result.output


def test_portal_login_json_warns_when_discord_missing(
    patched_build_client, fake_signer
) -> None:
    portal = _Portal(
        post_body={
            "provider": {
                "id": "m-7",
                "miner_hotkey": fake_signer.ss58_address,
                "provider_coldkey": "5CK",
                "discord_id": None,
                "created_at": "x",
                "updated_at": "x",
            },
            "token": _make_jwt(int(time.time()) + 3600),
        },
        get_body={"discord_id": None},
    )
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "--json", "portal", "login"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip())
    assert payload["data"]["discord_connected"] is False
    assert payload["data"]["extra_incentive_eligible"] is False
    assert payload["warnings"] == [
        {
            "code": "DISCORD_REQUIRED_FOR_EXTRA_INCENTIVES",
            "message": "Discord is not connected. No Discord = no extra incentives. Run `lium provider config connect-discord` to become eligible.",
        }
    ]


def test_portal_login_auth_error_returns_exit_2(patched_build_client) -> None:
    portal = _Portal(post_raises=ProviderAuthError("bad signature"))
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 2, result.output


def test_portal_whoami_renders_body(patched_build_client) -> None:
    portal = _Portal(get_body={"id": "m-1", "miner_hotkey": "5xxx"})
    patched_build_client(portal)

    runner = CliRunner()
    result = runner.invoke(
        provider_command,
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
        provider_id="m-1",
    )
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "portal", "logout"],
    )
    assert result.exit_code == 0, result.output
    assert tmp_token_store.load(fake_signer.ss58_address) is None


def test_portal_login_requires_hotkey(monkeypatch) -> None:
    runner = CliRunner()
    result = runner.invoke(provider_command, ["portal", "login"])
    assert result.exit_code == 1, result.output
    assert "ARG_INVALID" in result.output


def test_portal_generic_provider_error_returns_exit_3(patched_build_client) -> None:
    portal = _Portal(post_raises=ProviderError("server boom", code="PORTAL_SERVER_ERROR"))
    patched_build_client(portal)
    runner = CliRunner()
    result = runner.invoke(
        provider_command,
        ["--hotkey", "hk1", "portal", "login"],
    )
    assert result.exit_code == 3, result.output
