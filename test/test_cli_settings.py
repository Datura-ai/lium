import stat

from lium.cli.settings import ConfigManager


def test_api_api_key_reads_standard_sdk_env_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_API_KEY", "sdk-env-key")
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)

    config = ConfigManager()

    assert config.get("api.api_key") == "sdk-env-key"


def test_generated_env_key_takes_precedence_over_api_key_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_API_KEY", "sdk-env-key")
    monkeypatch.setenv("LIUM_API_API_KEY", "cli-env-key")

    config = ConfigManager()

    assert config.get("api.api_key") == "cli-env-key"


def test_saved_config_is_readable_by_the_owner_only(monkeypatch, tmp_path):
    """The file stores the API key, so its mode must not follow a permissive umask."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_API_KEY", raising=False)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)
    config = ConfigManager()

    config.set("api.api_key", "sk_written")

    assert stat.S_IMODE(config.config_file.stat().st_mode) == 0o600
