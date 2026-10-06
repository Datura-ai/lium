"""The SDK reads the same settings as the CLI.

Every CLI command builds a plain `Lium()`, which goes through `Config.load()`.
That used to ignore the `[ssh] key_path` that `lium init` writes and the
`LIUM_API_API_KEY` spelling the CLI accepts, so the key shown by `lium config
get` and the key actually used for API calls and SSH could differ.
"""

from pathlib import Path

import pytest

from lium.sdk import Config
from lium.sdk import config as sdk_config

ENV_KEY = "env-key-0123456789abcdef-tail"
SECTION_ENV_KEY = "section-env-key-0123456789abcdef"
FILE_KEY = "file-key-0123456789abcdef-tail"


@pytest.fixture
def home(monkeypatch, tmp_path):
    """Empty home, empty environment, config file under our control."""
    for var in ("LIUM_API_KEY", "LIUM_API_API_KEY", "LIUM_SSH_KEY_PATH"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))  # Path.expanduser reads $HOME, not Path.home
    config_file = tmp_path / ".lium" / "config.ini"
    monkeypatch.setattr(sdk_config, "config_file_path", lambda: config_file)
    return tmp_path


def _write_config(home: Path, body: str) -> Path:
    config_file = home / ".lium" / "config.ini"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(body)
    return config_file


# --- API key ---------------------------------------------------------------------------------

def test_section_env_var_is_read_like_the_cli_does(home, monkeypatch):
    monkeypatch.setenv("LIUM_API_API_KEY", SECTION_ENV_KEY)

    assert sdk_config.resolve_api_key() == (SECTION_ENV_KEY, "env:LIUM_API_API_KEY")


def test_section_env_var_wins_over_lium_api_key_as_in_config_manager(home, monkeypatch):
    monkeypatch.setenv("LIUM_API_API_KEY", SECTION_ENV_KEY)
    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)

    assert sdk_config.resolve_api_key() == (SECTION_ENV_KEY, "env:LIUM_API_API_KEY")


def test_lium_api_key_still_beats_the_file(home, monkeypatch):
    _write_config(home, f"[api]\napi_key = {FILE_KEY}\n")
    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)

    assert sdk_config.resolve_api_key() == (ENV_KEY, "env:LIUM_API_KEY")


def test_empty_env_var_is_ignored(home, monkeypatch):
    _write_config(home, f"[api]\napi_key = {FILE_KEY}\n")
    monkeypatch.setenv("LIUM_API_API_KEY", "")

    api_key, source = sdk_config.resolve_api_key()

    assert api_key == FILE_KEY and source.startswith("config:")


def test_precedence_matches_the_cli_config_manager(home, monkeypatch):
    """Same inputs, same answer from both implementations."""
    from lium.cli.settings import ConfigManager

    config_file = _write_config(home, f"[api]\napi_key = {FILE_KEY}\n")
    monkeypatch.setenv("LIUM_API_API_KEY", SECTION_ENV_KEY)
    monkeypatch.setenv("LIUM_API_KEY", ENV_KEY)
    cli = ConfigManager()  # Path.home() is patched, so it reads the same file
    assert cli.config_file == config_file

    assert sdk_config.resolve_api_key()[0] == cli.get("api.api_key")


# --- SSH key ---------------------------------------------------------------------------------

def test_ssh_env_var_wins(home, monkeypatch):
    _write_config(home, "[ssh]\nkey_path = /elsewhere/key\n")
    monkeypatch.setenv("LIUM_SSH_KEY_PATH", "~/.ssh/from_env")

    path, source = sdk_config.resolve_ssh_key_path()

    assert path == home / ".ssh" / "from_env"
    assert source == "env:LIUM_SSH_KEY_PATH"


def test_configured_key_path_is_used_and_tilde_expanded(home):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("default key that must NOT win\n")
    config_file = _write_config(home, "[ssh]\nkey_path = ~/.ssh/lium_key\n")

    path, source = sdk_config.resolve_ssh_key_path()

    assert path == home / ".ssh" / "lium_key"
    assert source == f"config:{config_file} [ssh] key_path"


def test_a_configured_but_missing_key_is_kept_not_replaced(home):
    """Failing on the key the user chose beats silently using another one."""
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_rsa").write_text("x\n")
    _write_config(home, "[ssh]\nkey_path = /nowhere/missing_key\n")

    path, _ = sdk_config.resolve_ssh_key_path()

    assert path == Path("/nowhere/missing_key")


def test_default_probing_order_is_unchanged(home):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ecdsa").write_text("x\n")
    (home / ".ssh" / "id_rsa").write_text("x\n")

    path, source = sdk_config.resolve_ssh_key_path()

    assert path == home / ".ssh" / "id_rsa"
    assert source == f"default:{home / '.ssh' / 'id_rsa'}"


def test_no_key_anywhere(home):
    assert sdk_config.resolve_ssh_key_path() == (None, None)


# --- Config.load ----------------------------------------------------------------------------

def test_config_load_carries_both_sources(home, monkeypatch):
    config_file = _write_config(home, f"[api]\napi_key = {FILE_KEY}\n\n[ssh]\nkey_path = ~/.ssh/lium_key\n")

    config = Config.load()

    assert config.api_key == FILE_KEY
    assert config.api_key_source == f"config:{config_file} [api] api_key"
    assert config.ssh_key_path == home / ".ssh" / "lium_key"
    assert config.ssh_key_source == f"config:{config_file} [ssh] key_path"


def test_config_load_reads_the_public_key_next_to_the_configured_private_key(home):
    (home / ".ssh").mkdir()
    (home / ".ssh" / "lium_key.pub").write_text("ssh-ed25519 AAAA configured\n")
    (home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 BBBB default\n")
    (home / ".ssh" / "id_ed25519").write_text("x\n")
    _write_config(home, f"[api]\napi_key = {FILE_KEY}\n\n[ssh]\nkey_path = ~/.ssh/lium_key\n")

    assert Config.load().ssh_public_keys == ["ssh-ed25519 AAAA configured"]


def test_a_dotted_configured_key_name_reads_its_own_pub_file(home):
    """`~/.ssh/lium.ed25519` pairs with `lium.ed25519.pub`, not `lium.pub`."""
    (home / ".ssh").mkdir()
    (home / ".ssh" / "lium.ed25519").write_text("x\n")
    (home / ".ssh" / "lium.ed25519.pub").write_text("ssh-ed25519 CCCC dotted\n")
    (home / ".ssh" / "lium.pub").write_text("ssh-ed25519 DDDD wrong-file\n")
    _write_config(home, f"[api]\napi_key = {FILE_KEY}\n\n[ssh]\nkey_path = ~/.ssh/lium.ed25519\n")

    assert Config.load().ssh_public_keys == ["ssh-ed25519 CCCC dotted"]


def test_explicit_config_has_no_ssh_source():
    assert Config(api_key="k").ssh_key_source is None
