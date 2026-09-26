"""`lium init` next to a saved key: the key is checked, and a dead one leads to a new login."""

import json

import pytest
import requests
import responses
from click.testing import CliRunner

from lium.cli import interactive
from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init import actions as init_actions
from lium.cli.init import command as init_command
from lium.cli.settings import ConfigManager
from lium.cli.utils import EXIT_PERMISSION_DENIED
from lium.sdk import LiumAuthError, LiumBudgetExceededError, LiumPermissionError, LiumScopeError, LiumServerError


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


class _Browser(list):
    """Records browser logins; each returns ``key`` (None: the login was aborted or refused)."""

    key = "sk_new"


@pytest.fixture
def browser(monkeypatch):
    logins = _Browser()

    def _browser_auth():
        logins.append(True)
        return logins.key

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
    LiumAuthError("Invalid API key"),                                                  # 401: expired or revoked
    LiumPermissionError("Permission denied: API key 'cli' belongs to a workspace that is gone or that its account has left"),
    LiumPermissionError("Permission denied: API key 'cli' was created by an account that is no longer a member of its workspace"),
], ids=["401", "403-workspace-gone", "403-creator-left"])
def test_a_rejected_saved_key_is_replaced_by_a_browser_login(home, api, browser, terminal, refusal):
    api.outcome = refusal

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "Your saved API key has expired or was revoked. Starting a new login" in result.output
    assert browser == [True]
    assert home.get("api.api_key") == "sk_new"


def test_a_rejected_key_survives_an_aborted_browser_login(home, api, browser, terminal):
    api.outcome = LiumAuthError("Invalid API key")
    browser.key = None

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 1
    assert browser == [True]
    assert home.get("api.api_key") == "sk_saved"


@pytest.mark.parametrize("outcome", [
    LiumScopeError("API key 'ci' does not have the 'read' scope", scope="read"),
    LiumBudgetExceededError("API key budget exceeded"),
    LiumPermissionError("Permission denied: Account is blocked. Contact support."),
    LiumPermissionError("Permission denied: <html><body>403 Forbidden</body></html>"),   # a WAF page
], ids=["scope", "budget-402", "account-blocked", "waf-html"])
def test_a_refusal_that_is_not_a_dead_key_keeps_the_key(home, api, browser, terminal, outcome):
    api.outcome = outcome

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert home.get("api.api_key") == "sk_saved"
    assert "expired or was revoked" not in result.output


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


@pytest.mark.parametrize("agent", ["no-tty", "noninteractive-env-at-a-terminal"])
def test_agent_mode_with_a_rejected_key_exits_6_without_a_browser(monkeypatch, home, api, browser, agent):
    if agent == "no-tty":
        monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: False)
    else:
        monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)
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


def test_no_browser_with_a_rejected_key_prints_the_url_and_keeps_the_key(monkeypatch, home, api, browser):
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    monkeypatch.setattr(init_actions, "init_auth", lambda: ("https://lium.io/auth?s=abc", "abc"))
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--no-browser"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert "lium init --session abc" in result.output
    assert home.get("api.api_key") == "sk_saved"          # replaced by the --session step, not before


def test_session_replaces_a_rejected_key_without_a_browser(monkeypatch, home, api, browser):
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: "sk_session")
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--session", "abc"])

    assert result.exit_code == 0, result.output
    assert browser == []
    assert home.get("api.api_key") == "sk_session"


def test_force_replaces_a_working_key_after_the_new_login(home, api, browser, terminal):
    result = CliRunner().invoke(cli, ["init", "--force"])

    assert result.exit_code == 0, result.output
    assert api.seen == []                              # --force does not ask the API first
    assert browser == [True]
    assert home.get("api.api_key") == "sk_new"


def test_force_with_an_aborted_browser_login_keeps_the_old_key(home, api, browser, terminal):
    browser.key = None

    result = CliRunner().invoke(cli, ["init", "--force"])

    assert result.exit_code == 1
    assert browser == [True]
    assert home.get("api.api_key") == "sk_saved"


def test_force_no_browser_keeps_the_key_until_the_session_is_approved(monkeypatch, home, api, browser):
    monkeypatch.setenv("LIUM_NONINTERACTIVE", "1")
    monkeypatch.setattr(init_actions, "init_auth", lambda: ("https://lium.io/auth?s=abc", "abc"))
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: "sk_session")

    printed = CliRunner().invoke(cli, ["init", "--force", "--no-browser"])
    assert printed.exit_code == 0, printed.output
    assert "lium init --session abc" in printed.output
    assert home.get("api.api_key") == "sk_saved"

    exchanged = CliRunner().invoke(cli, ["init", "--session", "abc"])
    assert exchanged.exit_code == 0, exchanged.output
    assert home.get("api.api_key") == "sk_session"


@pytest.mark.parametrize("args", [["init", "--force", "--session", "abc"], ["init", "--session", "abc"]])
def test_an_unapproved_session_keeps_the_old_key(monkeypatch, home, api, browser, args):
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: None)

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 1
    assert home.get("api.api_key") == "sk_saved"


def test_the_config_file_is_written_atomically_and_owner_only(home):
    home.set("api.api_key", "sk_second")

    assert home.config_file.read_text().count("sk_second") == 1
    assert oct(home.config_file.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in home.config_dir.iterdir() if p.name.endswith(".tmp")] == []


# The real SDK client and the platform's own answers (lium-platform utils/auth.py), so the sorting in
# Lium._raise_for_status / permission_error is exercised, not a stubbed exception.
_ME = "https://lium.io/api/users/me"


@pytest.fixture
def real_http(monkeypatch):
    monkeypatch.setenv("LIUM_BASE_URL", "https://lium.io/api")
    monkeypatch.setattr("lium.sdk.utils.time.sleep", lambda *_: None)
    with responses.RequestsMock() as mock:
        yield mock


@pytest.mark.parametrize("status,body,expected", [
    (200, {"balance": 3.5}, "valid"),
    (401, {"detail": "API key not found"}, "rejected"),
    (403, {"detail": "API key 'cli' belongs to a workspace that is gone or that its account has left"}, "rejected"),
    (403, {"detail": "API key 'cli' was created by an account that is no longer a member of its workspace"}, "rejected"),
    (403, {"detail": "API key 'ci' does not have the 'read' scope"}, "valid"),
    (403, {"detail": "Insufficient balance. Your balance is $0.00"}, "valid"),
    (403, {"detail": "Account is blocked. Contact support."}, "refused"),
    (403, "<html><body><h1>403 Forbidden</h1></body></html>", "refused"),
    (429, {"detail": "Too many requests"}, "unreachable"),
    (502, "<html>Bad gateway</html>", "unreachable"),
], ids=["200", "401-not-found", "403-workspace-gone", "403-creator-left", "403-scope", "403-balance",
        "403-blocked", "403-waf-html", "429", "502"])
def test_the_platforms_answers_are_sorted(home, real_http, status, body, expected):
    kwargs = {"json": body} if isinstance(body, dict) else {"body": body, "content_type": "text/html"}
    real_http.add(responses.GET, _ME, status=status, **kwargs)

    check = init_actions.CheckSavedApiKeyAction("sk_saved").execute({})

    assert check.data["status"] == expected, check.error
