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
