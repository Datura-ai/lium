"""`lium init` next to a saved key: the key is checked, and a dead one leads to a new login."""

import json

import pytest
import requests
from click.testing import CliRunner

from lium.cli import interactive
from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init import actions as init_actions
from lium.cli.init import command as init_command
from lium.cli.settings import ConfigManager
from lium.cli.utils import EXIT_PERMISSION_DENIED
from lium.sdk import LiumAuthError, LiumPermissionError, LiumScopeError, LiumServerError


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in ("LIUM_API_KEY", "LIUM_API_API_KEY", "LIUM_WORKSPACE", "LIUM_OUTPUT", "LIUM_NONINTERACTIVE"):
        monkeypatch.delenv(name, raising=False)
    fresh = ConfigManager()
    monkeypatch.setattr(init_actions, "config", fresh)
    monkeypatch.setattr(init_command, "config", fresh)
    fresh.set("api.api_key", "sk_saved")
    return fresh


@pytest.fixture(autouse=True)
def ssh_setup_ok(monkeypatch):
    monkeypatch.setattr(
        init_actions.SetupSshKeyAction, "execute",
        lambda self, ctx: ActionResult(ok=True, data={"already_configured": True}),
    )


class _Client:
    """Stands in for lium.sdk.Lium: records the key it was built with, answers /users/me as told."""

    seen: list = []
    outcome: object = 12.5

    def __init__(self, config, source="sdk"):
        self.config = config
        _Client.seen.append(config.api_key)

    def balance(self):
        if isinstance(_Client.outcome, Exception):
            raise _Client.outcome
        return _Client.outcome


@pytest.fixture
def api(monkeypatch):
    _Client.seen = []
    _Client.outcome = 12.5
    monkeypatch.setattr("lium.sdk.Lium", _Client)
    return _Client


@pytest.fixture
def browser(monkeypatch):
    """Records browser logins; each one returns a fresh key."""
    logins = []

    def _browser_auth():
        logins.append(True)
        return "sk_new"

    monkeypatch.setattr(init_actions, "browser_auth", _browser_auth)
    return logins


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)


def test_a_valid_saved_key_is_kept_and_no_login_runs(home, api, browser, terminal):
    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_saved"]
    assert browser == []
    assert home.get("api.api_key") == "sk_saved"
    assert "API key already saved" in result.output


@pytest.mark.parametrize("refusal", [
    LiumAuthError("Invalid API key"),                 # 401: expired or revoked
    LiumPermissionError("API key is disabled"),       # 403
], ids=["401", "403"])
def test_a_refused_saved_key_is_replaced_by_a_browser_login(home, api, browser, terminal, refusal):
    api.outcome = refusal

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "Your saved API key has expired or was revoked. Starting a new login" in result.output
    assert browser == [True]
    assert home.get("api.api_key") == "sk_new"


def test_a_scope_refusal_is_not_a_dead_key(home, api, browser, terminal):
    api.outcome = LiumScopeError("API key 'ci' does not have the 'read' scope", scope="read")

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert home.get("api.api_key") == "sk_saved"


@pytest.mark.parametrize("outcome", [
    requests.ConnectionError("Connection refused"),
    LiumServerError("Server error: 502"),
], ids=["connection", "5xx"])
def test_an_unreachable_api_keeps_the_key_and_skips_the_login(home, api, browser, terminal, outcome):
    api.outcome = outcome

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert home.get("api.api_key") == "sk_saved"
    assert "Could not check the saved API key" in result.output
    assert "Keeping the saved key" in result.output


def test_agent_mode_with_a_refused_key_exits_6_without_a_browser(monkeypatch, home, api, browser):
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    monkeypatch.setenv("LIUM_OUTPUT", "json")
    monkeypatch.setattr(init_actions, "init_auth", lambda: pytest.fail("no auth session must be requested"))
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == EXIT_PERMISSION_DENIED == 6
    assert browser == []
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "saved_key_rejected"
    assert error["exit_code"] == 6
    assert "lium init --force --no-browser" in error["hint"]
    assert home.get("api.api_key") == "sk_saved"


def test_session_replaces_a_refused_key_without_a_browser(monkeypatch, home, api, browser):
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: "sk_session")
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--session", "abc"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert home.get("api.api_key") == "sk_session"


def test_force_discards_a_working_key_and_logs_in_again(home, api, browser, terminal):
    result = CliRunner().invoke(cli, ["init", "--force"])

    assert result.exit_code == 0, result.output
    assert api.seen == []                              # --force does not ask the API first
    assert browser == [True]
    assert home.get("api.api_key") == "sk_new"
