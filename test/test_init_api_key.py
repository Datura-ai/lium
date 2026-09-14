"""`lium init --api-key` — headless auth for an agent that already holds a key."""

import json
import stat

import pytest
import requests
from click.testing import CliRunner

from lium.cli import interactive
from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init import actions as init_actions
from lium.cli.init import command as init_command
from lium.cli.settings import ConfigManager
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR
from lium.sdk import LiumAuthError, LiumServerError


@pytest.fixture
def home(monkeypatch, tmp_path):
    """A fresh HOME with no ~/.lium and no key in the environment."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_WORKSPACE", raising=False)   # a requested workspace makes init refuse (lium#183)
    fresh = ConfigManager()
    monkeypatch.setattr(init_actions, "config", fresh)
    monkeypatch.setattr(init_command, "config", fresh)
    return fresh


@pytest.fixture
def ssh_setup_ok(monkeypatch, home):
    def _execute(self, ctx):
        home.set("ssh.key_path", str(home.config_dir.parent / ".ssh" / "id_ed25519"))
        return ActionResult(ok=True, data={"already_configured": False})

    monkeypatch.setattr(init_actions.SetupSshKeyAction, "execute", _execute)


class _Client:
    """Stands in for lium.sdk.Lium: records the key it was built with, answers /users/me as told."""

    seen: list = []
    sources: list = []
    configs: list = []
    outcome: object = 12.5

    def __init__(self, config, source="sdk"):
        self.config = config
        _Client.seen.append(config.api_key)
        _Client.sources.append(source)
        _Client.configs.append(config)

    def balance(self):
        if isinstance(_Client.outcome, Exception):
            raise _Client.outcome
        return _Client.outcome


@pytest.fixture
def api(monkeypatch):
    _Client.seen = []
    _Client.sources = []
    _Client.configs = []
    _Client.outcome = 12.5
    monkeypatch.setattr("lium.sdk.Lium", _Client)
    return _Client


def test_api_key_is_checked_then_saved_and_ssh_is_set_up(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_good"]                      # checked with the key that was passed, not the env
    assert home.get("api.api_key") == "sk_good"
    assert stat.S_IMODE(home.config_file.stat().st_mode) == 0o600
    assert "saved to" in result.output and "SSH key:" in result.output


def test_report_shows_a_bracketed_config_path_literally(monkeypatch, tmp_path, ssh_setup_ok, api):
    """The paths init prints are the user's text: a HOME like `~/agents/run[v3]` puts `[v3]` in the config path, and
    Rich reads `[v3]`-style brackets as markup (dropped, or a MarkupError for `[/x]`). Escaped at the sink, the
    line shows the path as it is."""
    monkeypatch.setenv("HOME", str(tmp_path / "run[v3]"))
    for name in ("LIUM_API_KEY", "LIUM_API_API_KEY", "LIUM_WORKSPACE"):
        monkeypatch.delenv(name, raising=False)
    fresh = ConfigManager()
    monkeypatch.setattr(init_actions, "config", fresh)
    monkeypatch.setattr(init_command, "config", fresh)

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good"])

    assert result.exit_code == 0, result.output
    assert "run[v3]/.lium/config.ini" in result.output.replace("\n", "")


def test_api_key_json_names_source_config_and_ssh_key(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["ok"] is True
    assert payload["saved_from"] == "flag"
    # the same value `lium whoami --json` / `lium balance --json` print: where the next command reads the key
    assert payload["api_key_source"] == f"config:{home.config_file} [api] api_key"
    assert payload["env_key"] is None
    assert payload["config_path"] == str(home.config_file)
    assert payload["ssh_key_path"].endswith("id_ed25519")


def test_a_refused_key_is_not_saved_and_exits_3(home, ssh_setup_ok, api):
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_bad"])

    assert result.exit_code == EXIT_API_ERROR
    assert home.get("api.api_key") is None
    assert not home.config_file.exists()
    assert "refused" in result.output
    # the probe client is labelled so an auth error names the flag, not the SDK's "explicit"
    assert api.configs[0].api_key_source == "--api-key"


def test_a_refused_key_under_json_is_an_envelope_on_stderr(home, ssh_setup_ok, api):
    api.outcome = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_bad", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_api_key"
    # nothing was saved, so the generic "lium config get api.api_key shows which one is used" would mislead
    assert "nothing was saved" in error["hint"] and "lium.io/api-keys" in error["hint"]


def test_the_check_uses_the_flag_key_not_the_environment_or_the_file(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")
    home.set("api.api_key", "sk_file")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_flag"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_flag"]
    assert api.sources == ["cli"]
    assert home.get_all()["api"]["api_key"] == "sk_flag"
    assert "LIUM_API_KEY is set and wins" in result.output

    payload = json.loads(CliRunner().invoke(cli, ["init", "--api-key", "sk_flag2", "--json"]).output)
    assert payload["env_key"] == "LIUM_API_KEY"      # the JSON caller sees the same warning as a field
    assert payload["saved_from"] == "flag"
    assert payload["api_key_source"] == "env:LIUM_API_KEY"   # the exported key is what the next command uses


@pytest.mark.parametrize("outcome", [
    requests.ConnectionError("Connection refused"),      # the SDK lets transport errors through raw
    LiumServerError("Server error: 502"),                # a non-auth answer says nothing about the key
])
def test_an_unreachable_api_does_not_save_the_key(home, ssh_setup_ok, api, outcome):
    api.outcome = outcome

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_unknown", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert home.get("api.api_key") is None
    assert json.loads(result.output)["error"]["code"] == "api_unreachable"


def test_an_empty_key_is_refused_without_a_request(monkeypatch, home, ssh_setup_ok, api):
    """`--api-key "$KEY"` with KEY unset must not fall through to the browser flow."""
    def _no_browser(self, ctx):
        raise AssertionError("browser flow must not run for --api-key ''")

    monkeypatch.setattr(init_actions.SetupApiKeyAction, "execute", _no_browser)

    result = CliRunner().invoke(cli, ["init", "--api-key", "", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert json.loads(result.output)["error"]["code"] == "empty_api_key"
    assert api.seen == []
    assert home.get("api.api_key") is None


def test_a_key_with_a_newline_inside_is_refused_without_a_request_and_not_echoed(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_ab\rcd", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.output)["error"]["code"] == "invalid_api_key"
    assert "sk_ab" not in result.output
    assert api.seen == []
    assert home.get("api.api_key") is None


def test_api_key_replaces_a_previously_saved_key(home, ssh_setup_ok, api):
    home.set("api.api_key", "sk_old")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_new"])

    assert result.exit_code == 0, result.output
    assert home.get("api.api_key") == "sk_new"


def test_api_key_with_a_browser_option_is_refused(home, ssh_setup_ok, api):
    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good", "--no-browser"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert api.seen == []


def test_env_key_skips_the_browser_and_saves_nothing(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")

    def _no_browser(self, ctx):
        raise AssertionError("browser flow must not run when LIUM_API_KEY is set")

    monkeypatch.setattr(init_actions.SetupApiKeyAction, "execute", _no_browser)

    result = CliRunner().invoke(cli, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["saved_from"] == "env"
    assert payload["api_key_source"] == "env:LIUM_API_KEY"   # what `lium whoami --json` says too
    assert payload["env_key"] == "LIUM_API_KEY"
    assert home.get_all().get("api", {}).get("api_key") is None   # the key is not written
    assert home.get("ssh.key_path")                              # the SSH half still happens
    assert api.seen == []


def test_env_key_text_names_the_variable_and_the_file(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 0, result.output
    assert "LIUM_API_KEY" in result.output and "not written to" in result.output


def test_the_cli_alias_variable_is_a_key_source_like_the_sdk_reads_it(monkeypatch, home, ssh_setup_ok, api):
    """Since lium#136 the SDK's `Config.load()` reads LIUM_API_API_KEY before LIUM_API_KEY, so every command works
    with only the alias exported; an init that refused it (the pre-#136 `unsupported_env_key`) sent the caller to
    fix something that was not broken. It takes the env path: nothing saved, no request, browser not opened."""
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_alias")
    monkeypatch.setattr(init_actions, "browser_auth", lambda: pytest.fail("browser must not open"))

    result = CliRunner().invoke(cli, ["init", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["saved_from"] == "env" and payload["env_key"] == "LIUM_API_API_KEY"
    assert payload["api_key_source"] == "env:LIUM_API_API_KEY"   # what `lium whoami --json` prints too
    assert home.get_all().get("api", {}).get("api_key") is None
    assert api.seen == []


def test_api_key_with_the_alias_exported_warns_that_it_wins(monkeypatch, home, ssh_setup_ok, api):
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_alias")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good"])

    assert result.exit_code == 0, result.output
    assert api.seen == ["sk_good"] and home.get_all()["api"]["api_key"] == "sk_good"   # the file, not the env
    assert "LIUM_API_API_KEY is set and wins" in result.output


def test_init_refuses_when_a_workspace_is_requested_for_the_command(monkeypatch, home, ssh_setup_ok, api):
    """With LIUM_WORKSPACE (or the global -w) set, every command runs with the key saved for that workspace and
    nothing else (lium#183). `init` writes the account key, so an init that exited 0 here would be followed by
    `No API key is saved for workspace …` on the next command: refuse before checking or saving anything."""
    monkeypatch.setenv("LIUM_WORKSPACE", "research")

    for argv in (["init", "--api-key", "sk_good", "--json"], ["-w", "research", "init", "--api-key", "sk_good", "--json"]):
        result = CliRunner().invoke(cli, argv)

        assert result.exit_code == EXIT_CONFIGURATION_ERROR, argv
        envelope = json.loads(result.stderr)
        assert envelope["error"]["code"] == "invalid_arguments"
        assert "lium keys create <name> --workspace research --save" in envelope["error"]["message"]
        assert api.seen == [] and home.get_all().get("api", {}).get("api_key") is None


def test_api_key_source_follows_the_active_workspace_key(home, ssh_setup_ok, api):
    """`lium workspaces use research` saved a key under [workspace.research] and made it the default: the next
    command reads THAT key (lium#183), not the [api] key init just saved. `api_key_source` must say so — the same
    value `lium whoami --json` prints — and the text warns."""
    home.set("workspaces.active", "research")
    home.set_in_section("workspace.research", "api_key", "sk_research")

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["saved_from"] == "flag" and home.get("api.api_key") == "sk_good"
    assert payload["api_key_source"] == f"config:{home.config_file} [workspace.research] api_key"
    assert payload["active_workspace"] == "research"

    result = CliRunner().invoke(cli, ["init", "--api-key", "sk_good"])

    assert result.exit_code == 0, result.output
    assert "`lium workspaces use research` is in effect" in result.output


def test_the_alias_next_to_a_saved_key_reports_the_environment(monkeypatch, home, ssh_setup_ok, api):
    """LIUM_API_API_KEY exported next to a saved [api] key: the SDK reads the variable first (lium#136), so init
    says the environment is the source — as it does for LIUM_API_KEY — instead of 'already saved'; piped and at a
    terminal alike, and no auth session is requested."""
    monkeypatch.setenv("LIUM_API_API_KEY", "sk_alias")
    home.set("api.api_key", "sk_file")
    monkeypatch.setattr(init_actions, "init_auth", lambda: pytest.fail("no auth session must be requested"))

    piped = CliRunner().invoke(cli, ["init"])
    assert piped.exit_code == 0, piped.output
    assert "Using the API key from LIUM_API_API_KEY" in piped.output

    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)
    monkeypatch.setattr(init_actions.SetupApiKeyAction, "execute", lambda self, ctx: pytest.fail("browser flow must not run"))
    at_terminal = CliRunner().invoke(cli, ["init"])
    assert at_terminal.exit_code == 0, at_terminal.output
    assert "Using the API key from LIUM_API_API_KEY" in at_terminal.output
    assert home.get_all()["api"]["api_key"] == "sk_file"   # the file is left alone


def test_env_key_wins_over_session_and_no_browser(monkeypatch, home, ssh_setup_ok, api):
    """With LIUM_API_KEY exported the session/URL actions did nothing and said nothing (already
    configured); now every init flow reports the environment as the source."""
    monkeypatch.setenv("LIUM_API_KEY", "sk_env")
    monkeypatch.setattr(init_actions, "init_auth", lambda: pytest.fail("no auth session must be requested"))
    monkeypatch.setattr(init_actions, "poll_auth", lambda *a, **k: pytest.fail("no session must be polled"))

    for args in (["init", "--session", "abc", "--json"], ["init", "--no-browser", "--json"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == 0, (args, result.output)
        assert json.loads(result.output)["saved_from"] == "env", args


def test_json_needs_a_key_source(home, ssh_setup_ok, api):
    """The browser flows print for a person; --json with them would mix text and JSON on stdout."""
    for args in (["init", "--json"], ["init", "--no-browser", "--json"], ["init", "--session", "abc", "--json"]):
        result = CliRunner().invoke(cli, args)
        assert result.exit_code == EXIT_CONFIGURATION_ERROR, args
        assert json.loads(result.output)["error"]["code"] == "invalid_arguments", args
    assert api.seen == []


def test_browser_failure_names_the_headless_way(monkeypatch, home, ssh_setup_ok, api):
    # the browser flow only runs at a terminal (lium#124); CliRunner's stdin is not one
    monkeypatch.setattr(interactive, "stdin_is_terminal", lambda: True)
    monkeypatch.setattr(init_actions, "init_auth", lambda: pytest.fail("no auth session must be requested"))
    monkeypatch.setattr(
        init_actions.SetupApiKeyAction, "execute",
        lambda self, ctx: ActionResult(ok=False, data={}, error="Authentication failed"),
    )

    result = CliRunner().invoke(cli, ["init"])

    assert result.exit_code == 1
    assert "--api-key" in result.output and "LIUM_API_KEY" in result.output


def test_config_dir_is_created_with_its_parents(monkeypatch, tmp_path):
    """A fresh container's HOME may not exist yet; every command imports the config at start."""
    monkeypatch.setenv("HOME", str(tmp_path / "not-yet"))

    manager = ConfigManager()

    assert manager.config_dir == tmp_path / "not-yet" / ".lium"
    assert manager.config_dir.is_dir()
