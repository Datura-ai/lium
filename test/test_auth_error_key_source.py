"""An auth or balance error must name the key it was raised for.

`lium up` can fail with "insufficient balance" while `lium balance` shows
plenty: the two commands ran in different shells and resolved different keys
(environment variable versus config file). Neither error nor output said which
key was used, so the caller had nothing to compare.
"""


from types import SimpleNamespace
import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli import balance as balance_module
from lium.cli import settings
from lium.sdk import Config, Lium, LiumAuthError, LiumPermissionError
from lium.sdk import config as sdk_config

ENV_KEY = "env-key-0123456789abcdef-tail"
FILE_KEY = "file-key-0123456789abcdef-tail"


@pytest.fixture
def key_sources(monkeypatch, tmp_path):
    """No key in the environment, a config file in a temp home."""
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)  # it outranks LIUM_API_KEY since DAH-2896
    config_file = tmp_path / "config.ini"
    monkeypatch.setattr(sdk_config, "config_file_path", lambda: config_file)
    return config_file


def _write_config(config_file, api_key):
    config_file.write_text(f"[api]\napi_key = {api_key}\n")


def test_env_key_wins_and_is_named_as_the_source(key_sources, monkeypatch):
    _write_config(key_sources, FILE_KEY)
    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)

    assert sdk_config.resolve_api_key() == (ENV_KEY, "env:LIUM_API_KEY")


def test_config_file_key_names_the_file_and_option(key_sources):
    _write_config(key_sources, FILE_KEY)

    api_key, source = sdk_config.resolve_api_key()

    assert api_key == FILE_KEY
    assert source == f"config:{key_sources} [api] api_key"


def test_no_key_anywhere_resolves_to_nothing(key_sources):
    assert sdk_config.resolve_api_key() == (None, None)


def test_config_load_records_the_source(key_sources, monkeypatch):
    _write_config(key_sources, FILE_KEY)

    config = Config.load()

    assert config.api_key == FILE_KEY
    assert config.api_key_source.startswith("config:")
    assert config.api_key_description == f"key file-k…tail from config:{key_sources} [api] api_key"


@pytest.mark.parametrize(
    "api_key, fingerprint",
    [("abcdef0123456789wxyz", "abcdef…wxyz"), ("short", "***"), ("", "none"), (None, "none")],
)
def test_fingerprint_shows_only_the_ends(api_key, fingerprint):
    assert sdk_config.api_key_fingerprint(api_key) == fingerprint


def test_an_explicit_config_says_so():
    config = Config(api_key="abcdef0123456789wxyz")

    assert config.api_key_description == "key abcdef…wxyz from explicit"


class _Response:
    ok = False
    text = ""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _client_receiving(monkeypatch, response):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: response)
    return Lium(Config(api_key=ENV_KEY, api_key_source="env:LIUM_API_KEY"))


def test_401_names_the_key_and_its_source(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(401))

    with pytest.raises(LiumAuthError) as raised:
        client._request("GET", "/pods")

    assert "key env-ke…tail from env:LIUM_API_KEY" in str(raised.value)


def test_403_names_the_key_and_its_source(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "User is not verified"}))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("GET", "/pods")

    assert "User is not verified" in str(raised.value)
    assert "key env-ke…tail from env:LIUM_API_KEY" in str(raised.value)


def test_insufficient_balance_shows_required_and_available(monkeypatch):
    """The amounts come from the platform's message (the one parser, permission_error); the key is named."""
    client = _client_receiving(monkeypatch, _Response(403, {
        "detail": "Insufficient balance. This node costs $2.00/hour, so renting it requires at least $12.50 "
                  "(15 minutes of runtime). Your balance is $3.00.",
    }))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert (raised.value.required, raised.value.available) == (12.5, 3.0)
    assert "from env:LIUM_API_KEY" in str(raised.value)


def test_insufficient_balance_without_numbers_stays_plain(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "insufficient balance"}))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert "required" not in str(raised.value)
    assert "insufficient balance (key" in str(raised.value)


def test_a_403_without_a_balance_word_gets_no_balance_detail(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "Forbidden", "balance": 99}))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("GET", "/pods")

    assert "available" not in str(raised.value)


def test_logs_401_names_the_key(monkeypatch):
    # logs() goes through _request like every other call, so the 401 is decided by the
    # same requests.request patch as the tests above; the response is a context manager
    # because a streamed response is closed on the error path.
    class _Unauthorized(_Response):
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

        def close(self):
            return None

    client = _client_receiving(monkeypatch, _Unauthorized(401))

    with pytest.raises(LiumAuthError, match="from env:LIUM_API_KEY"):
        list(client.logs("pod-1"))


class _FakeBalanceLium:
    def __init__(self, *args, **kwargs):
        self.config = Config(api_key=ENV_KEY, api_key_source="env:LIUM_API_KEY")

    def balance(self):
        return 42.5


def test_balance_prints_the_key_source(monkeypatch):
    monkeypatch.setattr(balance_module, "Lium", _FakeBalanceLium)

    result = CliRunner().invoke(cli, ["balance"])

    assert result.exit_code == 0, result.output
    assert "42.5 USD" in result.output
    assert "key env-ke…tail from env:LIUM_API_KEY" in result.output


def test_balance_json_carries_the_key_source(monkeypatch):
    import json

    monkeypatch.setattr(balance_module, "Lium", _FakeBalanceLium)

    result = CliRunner().invoke(cli, ["balance", "--json"])

    payload = json.loads(result.stdout)
    assert payload["balance_usd"] == 42.5
    assert payload["api_key_source"] == "env:LIUM_API_KEY"
    assert payload["api_key_fingerprint"] == "env-ke…tail"
    assert ENV_KEY not in result.stdout


def test_config_get_prints_where_the_key_came_from(monkeypatch):
    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)

    result = CliRunner().invoke(cli, ["config", "get", "api.api_key"])

    assert result.exit_code == 0, result.output
    assert "from env:LIUM_API_KEY" in result.output
    assert ENV_KEY not in result.output


def test_config_manager_source_follows_get_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)
    manager = settings.ConfigManager.__new__(settings.ConfigManager)
    manager.config_dir = tmp_path
    manager.config_file = tmp_path / "config.ini"
    manager.config_file.write_text(f"[api]\napi_key = {FILE_KEY}\n")
    manager._config = manager._load_config()

    assert manager.get_source("api.api_key") == f"config:{manager.config_file} [api] api_key"
    assert manager.get_source("ui.theme") is None

    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)
    assert manager.get_source("api.api_key") == "env:LIUM_API_KEY"


class _FakePsLium:
    pods = []
    workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `ps` reads it for its workspace line

    def __init__(self, *args, **kwargs):
        self.config = Config(api_key=ENV_KEY, api_key_source="env:LIUM_API_KEY")

    def ps(self):
        return list(self.pods)


def _ps(monkeypatch, pods, *args):
    from lium.cli.ps import command as ps_module

    monkeypatch.setattr(_FakePsLium, "pods", pods)
    monkeypatch.setattr(ps_module, "Lium", _FakePsLium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["ps", *args])


def test_ps_empty_list_names_the_key_source(monkeypatch):
    result = _ps(monkeypatch, [])

    assert result.exit_code == 0, result.output
    assert "No active pods" in result.output
    assert "key env-ke…tail from env:LIUM_API_KEY" in result.output
    assert ENV_KEY not in result.output


def test_ps_table_names_the_key_source(monkeypatch):
    from lium.sdk import PodInfo

    pod = PodInfo(id="pod-1", name="alpha", status="RUNNING", huid="alpha-1", ssh_cmd=None, ports={},
                  created_at="2026-09-06T00:00:00", updated_at="2026-09-06T00:00:00", executor=None, template={},
                  removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None)
    result = _ps(monkeypatch, [pod])

    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "key env-ke…tail from env:LIUM_API_KEY" in result.output


def test_ps_json_keeps_stdout_a_bare_array_and_stderr_empty(monkeypatch):
    """The machine contract (docs/exit-codes.md): a bare array on stdout and, on failure, one JSON object on
    stderr. A successful `ps --format json` must leave stderr empty, so no account line there."""
    import json

    result = _ps(monkeypatch, [], "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == []
    assert result.stderr == ""
