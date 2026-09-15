"""DAH-2587: `lium signup` creates an account and stores the API key it mints."""

import json
import pathlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.cli import cli
from lium.cli.init.actions import SetupSshKeyAction
from lium.cli.signup import actions as signup_actions
from lium.cli.signup import command as signup_command


class FakeResponse:
    def __init__(self, status_code: int = 200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.fixture
def stored_config(monkeypatch):
    """Config double: empty to start, records what signup writes into it."""
    stored = {}

    class FakeConfig:
        def get(self, key, default=None):
            return stored.get(key, default)

        def set(self, key, value):
            stored[key] = value

    monkeypatch.setattr(signup_actions, "config", FakeConfig())
    return stored


@pytest.fixture
def ssh_setup_ok(monkeypatch):
    monkeypatch.setattr(
        "lium.cli.init.actions.SetupSshKeyAction.execute",
        lambda self, ctx: ActionResult(ok=True, data={"already_configured": True}),
    )


def test_signup_stores_key_read_back_from_keys_endpoint(monkeypatch, stored_config, ssh_setup_ok):
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if url.endswith("/users"):
            return FakeResponse(200, {"msg": "success"})
        return FakeResponse(200, {"token": "jwt-token"})

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResponse(200, [{"name": "Default", "key": "sk_minted"}])

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setattr(signup_actions.requests, "get", fake_get)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert stored_config["api.api_key"] == "sk_minted"
    assert [c.rsplit("/api", 1)[-1] for c in calls] == ["/users", "/users/login", "/keys"]


def test_signup_skips_dead_keys_and_stores_the_newest_live_one(monkeypatch, stored_config, ssh_setup_ok):
    """GET /keys lists revoked and expired rows too; a restored account carries several named "Default"."""
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"msg": "success"} if url.endswith("/users") else {"token": "jwt"}),
    )
    monkeypatch.setattr(signup_actions.requests, "get", lambda url, **kwargs: FakeResponse(200, [
        {"name": "Default", "key": "sk_revoked", "is_active": False, "created_at": "2026-08-04T10:00:00"},
        {"name": "Default", "key": "sk_expired", "is_active": True, "created_at": "2026-08-05T10:00:00",
         "expires_at": "2020-01-01T00:00:00"},
        {"name": "Default", "key": "sk_live", "is_active": True, "created_at": "2026-08-05T09:00:00"},
        {"name": "ci", "key": "sk_other", "is_active": True, "created_at": "2030-01-01T00:00:00"},
    ]))

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert stored_config["api.api_key"] == "sk_live"


def test_signup_takes_the_password_from_the_environment(monkeypatch, stored_config, ssh_setup_ok):
    """A password passed as a flag leaks into shell history and `ps`; the env var is the safer source."""
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return FakeResponse(200, {"api_key": "sk_inline"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setenv("LIUM_SIGNUP_PASSWORD", "env-pw")

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    assert sent["password"] == "env-pw"


def test_signup_uses_key_returned_by_signup_response(monkeypatch, stored_config, ssh_setup_ok):
    """A backend that returns the key inline makes login + GET /keys unnecessary."""
    posted = []

    def fake_post(url, **kwargs):
        posted.append(url)
        return FakeResponse(200, {"msg": "success", "api_key": "sk_inline"})

    def fail_get(url, **kwargs):
        raise AssertionError("GET /keys must be skipped when signup returns the key")

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setattr(signup_actions.requests, "get", fail_get)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert stored_config["api.api_key"] == "sk_inline"
    assert len(posted) == 1


def test_signup_json_reports_credentials_and_next_steps(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"msg": "success", "api_key": "sk_inline"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["api_key"] == "sk_inline"
    assert payload["email"] == "ada@example.com"
    assert payload["password"]
    assert any("verification link" in step for step in payload["next_steps"])
    # DAH-2587 dropped the email_verified gate from the rent endpoints — balance is the only gate now
    assert not any("renting is blocked" in step for step in payload["next_steps"])


def test_signup_reports_credentials_when_the_key_cannot_be_read_back(monkeypatch, stored_config, ssh_setup_ok):
    """The account exists server-side; without the password nobody can ever log into it."""
    def fake_post(url, **kwargs):
        if url.endswith("/users"):
            return FakeResponse(200, {"msg": "success"})
        return FakeResponse(200, {})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)
    monkeypatch.setattr(signup_actions.requests, "get", lambda url, **kwargs: FakeResponse(200, []))

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--password", "s3cret-pw"])

    assert result.exit_code != 0
    plain = " ".join(result.output.split())
    assert "s3cret-pw" in plain
    assert "ada@example.com" in plain
    assert "copy your API key from the dashboard" in plain


def test_signup_reports_credentials_when_the_request_times_out(monkeypatch, stored_config, ssh_setup_ok):
    """The backend mails the user inside the request, so a read timeout can still leave an account behind."""
    def timing_out_post(url, **kwargs):
        raise signup_actions.requests.Timeout("read timed out")

    monkeypatch.setattr(signup_actions.requests, "post", timing_out_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--password", "s3cret-pw"])

    assert result.exit_code != 0
    assert "s3cret-pw" in result.output
    assert "may have been created" in result.output


def test_signup_reports_credentials_when_the_connection_drops(monkeypatch, stored_config, ssh_setup_ok):
    """A reset connection can drop the response of a request the backend already acted on."""
    def failing_post(url, **kwargs):
        raise signup_actions.requests.ConnectionError("connection reset by peer")

    monkeypatch.setattr(signup_actions.requests, "post", failing_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--password", "s3cret-pw"])

    assert result.exit_code != 0
    plain = " ".join(result.output.split())
    assert "s3cret-pw" in plain
    assert "may have been created" in plain


def test_signup_json_error_envelope_carries_credentials(monkeypatch, stored_config, ssh_setup_ok):
    def timing_out_post(url, **kwargs):
        raise signup_actions.requests.Timeout("read timed out")

    monkeypatch.setattr(signup_actions.requests, "post", timing_out_post)

    result = CliRunner().invoke(
        cli, ["signup", "--email", "ada@example.com", "--password", "s3cret-pw", "--json"]
    )

    assert result.exit_code != 0
    envelope = json.loads(result.stderr)
    assert envelope["data"] == {"email": "ada@example.com", "password": "s3cret-pw"}


def test_signup_json_error_envelope_omits_credentials_when_no_account_was_created(
    monkeypatch, stored_config, ssh_setup_ok
):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(400, {"detail": "User already exists with the same email"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code != 0
    assert "data" not in json.loads(result.stderr)


def test_signup_announces_the_credit_only_when_it_was_granted(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"api_key": "sk_inline", "signup_credit_granted": True}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert "$5 signup credit was granted" in result.output


def test_signup_says_no_credit_was_granted_when_the_flag_is_false(monkeypatch, stored_config, ssh_setup_ok):
    """The password is fixed here: without --password, signup prints the one it generated, and a random
    password can contain "$5" (it did on one CI run), which is not the credit sentence this test is about."""
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"api_key": "sk_inline", "signup_credit_granted": False}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--password", "s3cret-pw"])

    assert result.exit_code == 0
    assert "No signup credit was granted" in result.output
    assert "$5 signup credit was granted" not in result.output


def test_a_generated_password_containing_dollar_five_is_not_mistaken_for_the_credit(
    monkeypatch, stored_config, ssh_setup_ok
):
    """The negative control for the test above: the password lium#129's CI run generated (`…O5$5cayT#Y`) is printed
    and contains "$5", while the credit sentence is still absent — the two must be told apart."""
    monkeypatch.delenv("LIUM_SIGNUP_PASSWORD", raising=False)  # the env var would replace the generated password
    monkeypatch.setattr(signup_command, "generate_password", lambda: "EyH@j&dQO5$5cayT#Y")
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"api_key": "sk_inline", "signup_credit_granted": False}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert "EyH@j&dQO5$5cayT#Y" in result.output  # the generated password is shown to the user
    assert "No signup credit was granted" in result.output
    assert "$5 signup credit was granted" not in result.output


def test_signup_asserts_nothing_about_the_credit_on_an_older_backend(monkeypatch, stored_config, ssh_setup_ok):
    """A backend without signup_credit_granted leaves the credit unknown."""
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"api_key": "sk_inline"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["signup_credit_granted"] is None
    assert not any("$5" in step or "No signup credit" in step for step in payload["next_steps"])


def test_signup_json_reports_the_credit_flag(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(200, {"api_key": "sk_inline", "signup_credit_granted": True}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output)["signup_credit_granted"] is True


def test_signup_generates_a_password_when_omitted(monkeypatch, stored_config, ssh_setup_ok):
    sent = {}

    def fake_post(url, **kwargs):
        sent.update(kwargs["json"])
        return FakeResponse(200, {"api_key": "sk_inline"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com", "--json"])

    assert result.exit_code == 0
    assert len(sent["password"]) == signup_actions.PASSWORD_LENGTH
    assert sent["name"] == "ada"
    assert json.loads(result.output)["password"] == sent["password"]


def test_signup_refuses_when_a_key_is_already_configured(monkeypatch, stored_config, ssh_setup_ok):
    stored_config["api.api_key"] = "sk_existing"

    def fail_post(url, **kwargs):
        raise AssertionError("signup must not call the API when a key is already configured")

    monkeypatch.setattr(signup_actions.requests, "post", fail_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert stored_config["api.api_key"] == "sk_existing"


def test_signup_names_the_env_var_when_the_key_comes_from_the_environment(
    monkeypatch, stored_config, ssh_setup_ok
):
    """'lium config unset' cannot clear a key that an env var supplies."""
    stored_config["api.api_key"] = "sk_from_env"
    monkeypatch.setenv("LIUM_API_KEY", "sk_from_env")

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "unset LIUM_API_KEY" in " ".join(result.output.split())


def test_signup_surfaces_backend_rejection(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(400, {"detail": "User already exists with the same email"}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "already exists" in result.output
    assert "api.api_key" not in stored_config


def test_signup_surfaces_rate_limit(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setattr(
        signup_actions.requests, "post",
        lambda url, **kwargs: FakeResponse(429, {"detail": "Too many requests."}),
    )

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code != 0
    assert "Too many signups" in result.output


def test_signup_targets_the_configured_base_url(monkeypatch, stored_config, ssh_setup_ok):
    monkeypatch.setenv("LIUM_BASE_URL", "https://staging.lium.io/api")
    seen = []

    def fake_post(url, **kwargs):
        seen.append(url)
        return FakeResponse(200, {"api_key": "sk_inline"})

    monkeypatch.setattr(signup_actions.requests, "post", fake_post)

    result = CliRunner().invoke(cli, ["signup", "--email", "ada@example.com"])

    assert result.exit_code == 0
    assert seen == ["https://staging.lium.io/api/users"]


def test_completion_notice_goes_to_stderr(tmp_path, monkeypatch, capsys):
    """The first run configures shell completions; that notice must not land in --json stdout."""
    from lium.cli import completion

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("SHELL", "/bin/zsh")

    completion.ensure_completion()

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Shell completions" in captured.err


def test_ssh_setup_creates_the_ssh_directory_when_missing(tmp_path, monkeypatch):
    """A fresh machine has no ~/.ssh; ssh-keygen fails unless it is created first."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    stored = {}
    monkeypatch.setattr(
        "lium.cli.init.actions.config",
        type("C", (), {"get": lambda self, k, d=None: stored.get(k, d),
                       "set": lambda self, k, v: stored.__setitem__(k, v)})(),
    )

    result = SetupSshKeyAction().execute({})

    assert result.ok, result.error
    assert (tmp_path / ".ssh" / "id_ed25519").exists()
    assert stored["ssh.key_path"] == str(tmp_path / ".ssh" / "id_ed25519")
