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
