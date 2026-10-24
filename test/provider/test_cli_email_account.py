"""``lium provider`` for an account made with e-mail (custody='lium'): no key, so no --hotkey."""

from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command

ACCOUNT = "acct-7f3a"  # what the portal calls miner_hotkey for a custodied account: an opaque id
SESSION = pyjwt.encode({"exp": int(time.time()) + 3600}, "k" * 32, algorithm="HS256")


class _FakeHTTP:
    calls: list = []

    def __init__(self, base_url, token_provider):
        self._token = token_provider

    def post(self, path, *, json_body=None, auth=True):
        self.calls.append(("POST", path, json_body))
        return {"miner": {"id": "p1", "miner_hotkey": ACCOUNT, "miner_coldkey": "", "created_at": "", "updated_at": ""}, "token": SESSION}

    def get(self, path, *, params=None, auth=True):
        self.calls.append(("GET", path, self._token()))
        return {"data": []}


@pytest.fixture
def portal(monkeypatch, tmp_path):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    monkeypatch.setattr("lium.provider.client.PortalHTTP", _FakeHTTP)
    _FakeHTTP.calls = []
    return _FakeHTTP.calls


def test_email_login_then_machine_requests_without_hotkey(portal, monkeypatch) -> None:
    monkeypatch.setenv("LIUM_PROVIDER_PASSWORD", "pw")
    login = CliRunner().invoke(provider_command, ["portal", "login", "--email", "a@example.com"])
    assert login.exit_code == 0, login.output
    assert portal[0] == ("POST", "/auth/login-email", {"email": "a@example.com", "password": "pw"})

    listed = CliRunner().invoke(provider_command, ["machine-request", "list"])
    assert listed.exit_code == 0, listed.output
    # the cached session of the signed-in account goes out as the bearer
    assert portal[-1] == ("GET", "/machine-requests", SESSION)


def test_no_hotkey_and_no_account_names_the_email_sign_in(portal) -> None:
    result = CliRunner().invoke(provider_command, ["machine-request", "list"])
    assert result.exit_code != 0
    assert "portal login --email" in result.output
    assert portal == []


class _TokenHTTP(_FakeHTTP):
    params: dict = {}

    def get(self, path, *, params=None, auth=True):
        self.calls.append(("GET", path, self._token()))
        _TokenHTTP.params[path] = params
        if path == "/auth/me":
            return {"provider_id": "p1", "miner_hotkey": ACCOUNT}
        return {"data": []}


@pytest.fixture
def token_portal(portal, monkeypatch):
    monkeypatch.setattr("lium.provider.client.PortalHTTP", _TokenHTTP)
    return portal


def test_api_token_from_env_is_the_bearer_and_the_account_comes_from_auth_me(token_portal, monkeypatch) -> None:
    monkeypatch.setenv("LIUM_PROVIDER_TOKEN", "lpk_abc")
    result = CliRunner().invoke(provider_command, ["node", "list"])
    assert result.exit_code == 0, result.output
    assert ("GET", "/auth/me", "lpk_abc") in token_portal
    # the node listing is scoped to the account id /auth/me returned, sent with the token
    assert token_portal[-1] == ("GET", "/executors", "lpk_abc")
    assert _TokenHTTP.params["/executors"]["miner_hotkey"] == ACCOUNT


def test_login_token_saves_it_for_later_commands(token_portal) -> None:
    saved = CliRunner().invoke(provider_command, ["portal", "login", "--token"], input="lpk_xyz\n")
    assert saved.exit_code == 0, saved.output
    assert ACCOUNT in saved.output
    listed = CliRunner().invoke(provider_command, ["billing", "list"])
    assert listed.exit_code == 0, listed.output
    assert token_portal[-1] == ("GET", "/provider-ledger/daily", "lpk_xyz")


def test_no_identity_error_names_both_ways_in(portal) -> None:
    result = CliRunner().invoke(provider_command, ["billing", "list"])
    assert result.exit_code != 0
    assert "LIUM_PROVIDER_TOKEN" in result.output and "portal login --email" in result.output
