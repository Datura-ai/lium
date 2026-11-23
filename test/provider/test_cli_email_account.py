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


@pytest.mark.parametrize("args", [["config", "set-email", "b@example.com"], ["config", "set-password"]])
def test_signing_config_commands_ask_for_the_hotkey_not_a_token(token_portal, monkeypatch, args) -> None:
    # set-email / set-password sign with the hotkey: a token must get a clear error, not a traceback
    monkeypatch.setenv("LIUM_PROVIDER_TOKEN", "lpk_abc")
    result = CliRunner().invoke(provider_command, args, input="x\nx\n")
    assert result.exit_code != 0
    assert "--hotkey" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


class _NodeHTTP(_FakeHTTP):
    """Records whose nodes `node list` asks for; `node rm` deletes by id."""

    def get(self, path, *, params=None, auth=True):
        self.calls.append(("GET", path, self._token(), (params or {}).get("miner_hotkey")))
        if path == "/auth/me":
            return {"provider_id": "p1", "miner_hotkey": ACCOUNT}
        return {"data": []}

    def delete(self, path, *, json_body=None, auth=True):
        self.calls.append(("DELETE", path, self._token()))
        return {}


@pytest.fixture
def keyed(portal, monkeypatch):
    """A config hotkey that resolves to a local key, as for a provider who also mines with a hotkey."""
    from lium.cli.settings import ConfigManager

    from .conftest import FakeKeypair

    monkeypatch.setattr("lium.provider.client.PortalHTTP", _NodeHTTP)
    monkeypatch.setattr("lium.provider.wallet.load_hotkey_keypair", lambda coldkey, hotkey: FakeKeypair(f"5{hotkey}"))
    monkeypatch.setenv("LIUM_PROVIDER_PASSWORD", "pw")
    ConfigManager().set("provider.coldkey", "ck")
    ConfigManager().set("provider.hotkey", "cfghk")
    return portal


def _email_login() -> None:
    result = CliRunner().invoke(provider_command, ["portal", "login", "--email", "a@example.com"])
    assert result.exit_code == 0, result.output


def _lists_account(calls) -> None:
    listed = CliRunner().invoke(provider_command, ["node", "list"])
    assert listed.exit_code == 0, listed.output
    assert calls[-1] == ("GET", "/executors", SESSION, ACCOUNT)


def test_email_login_makes_node_list_and_rm_act_on_the_account_not_the_config_hotkey(keyed) -> None:
    _email_login()
    listed = CliRunner().invoke(provider_command, ["node", "list"])
    assert listed.exit_code == 0, listed.output
    assert keyed[-1] == ("GET", "/executors", SESSION, ACCOUNT)

    removed = CliRunner().invoke(provider_command, ["--yes", "node", "rm", "ex-1"])
    assert removed.exit_code == 0, removed.output
    assert keyed[-1][0] == "DELETE" and keyed[-1][2] == SESSION
    # the hotkey setting is kept for the hotkey account
    from lium.cli.settings import ConfigManager

    assert ConfigManager().get("provider.hotkey") == "cfghk"


def test_explicit_hotkey_still_wins_after_an_email_login(keyed) -> None:
    _email_login()
    _lists_account(keyed)
    listed = CliRunner().invoke(provider_command, ["--hotkey", "flaghk", "node", "list"])
    assert listed.exit_code == 0, listed.output
    assert keyed[-1][:2] == ("GET", "/executors") and keyed[-1][3] == "5flaghk"


def test_a_later_hotkey_login_switches_back_to_the_hotkey(keyed) -> None:
    _email_login()
    _lists_account(keyed)
    login = CliRunner().invoke(provider_command, ["portal", "login"])
    assert login.exit_code == 0, login.output
    listed = CliRunner().invoke(provider_command, ["node", "list"])
    assert listed.exit_code == 0, listed.output
    assert keyed[-1][:2] == ("GET", "/executors") and keyed[-1][3] == "5cfghk"


def test_whoami_names_the_identity_commands_act_on(keyed) -> None:
    _email_login()
    who = CliRunner().invoke(provider_command, ["--json", "portal", "whoami"])
    assert who.exit_code == 0, who.output
    assert '"active_identity": "account"' in who.output


def test_logout_of_the_email_account_falls_back_to_the_config_hotkey(keyed) -> None:
    _email_login()
    _lists_account(keyed)
    out = CliRunner().invoke(provider_command, ["portal", "logout"])
    assert out.exit_code == 0, out.output
    listed = CliRunner().invoke(provider_command, ["node", "list"])
    assert listed.exit_code == 0, listed.output
    assert keyed[-1][:2] == ("GET", "/executors") and keyed[-1][3] == "5cfghk"


def test_status_after_an_email_login_reports_the_account(keyed, monkeypatch) -> None:
    # status runs on the signed-in account; it does not ask for the hotkey that is already configured
    monkeypatch.setattr("lium.provider.client._read_metagraph", lambda **kw: (False, []))
    _email_login()
    result = CliRunner().invoke(provider_command, ["status"])
    assert result.exit_code == 0, result.output
    assert f"hotkey={ACCOUNT}" in result.output and "portal=active" in result.output
    assert keyed[-1] == ("GET", "/executors", SESSION, ACCOUNT)


@pytest.mark.parametrize("args", [["config", "set-email", "b@example.com"], ["config", "set-password"]])
def test_signing_commands_after_an_email_login_say_why_the_config_hotkey_is_not_used(keyed, args) -> None:
    # they sign with a hotkey; the error names the configured one and how to use it, not "set a hotkey"
    _email_login()
    result = CliRunner().invoke(provider_command, args, input="x\nx\n")
    assert result.exit_code != 0
    assert "'cfghk'" in result.output and "portal login --email" in result.output
    assert "--hotkey cfghk" in result.output and "lium provider portal login" in result.output


def test_email_login_wins_over_an_exported_token_for_a_mutating_command(keyed, monkeypatch) -> None:
    monkeypatch.setenv("LIUM_PROVIDER_TOKEN", "lpk_abc")
    _email_login()
    removed = CliRunner().invoke(provider_command, ["--yes", "node", "rm", "ex-1"])
    assert removed.exit_code == 0, removed.output
    assert keyed[-1][0] == "DELETE" and keyed[-1][2] == SESSION
    assert not any(call[2] == "lpk_abc" for call in keyed)
