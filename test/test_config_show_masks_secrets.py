"""`lium config show` masks API keys and the session token (DAH-3445).

Regression: the command printed ``~/.lium/config.ini`` byte for byte, so ``[api] api_key``, every
workspace's ``api_key`` and ``[session] token`` landed in full in whatever kept the output (a CI log,
an agent transcript), while ``config get`` and ``config set`` masked the same values.
"""

import re

from click.testing import CliRunner

from lium.cli import settings
from lium.cli.cli import cli
from lium.cli.config.show.actions import mask_config_text

API_KEY = "lium-test-key-0123456789abcdefghijklmnop"
WORKSPACE_KEY = "sk_ws_zyxwvutsrqponmlkjihgfedcba9876"
SESSION_TOKEN = "eyJhbGciOiJIUzI1NiJ9.session-token-payload.signature"

CONFIG = f"""[api]
api_key = {API_KEY}

[ssh]
key_path = /home/me/.ssh/lium.ed25519

[workspace.team-a]
id = 7c1e
api_key = {WORKSPACE_KEY}

[session]
token = {SESSION_TOKEN}
# a comment that mentions token = keep-me
"""


def _run_show(monkeypatch, tmp_path, text):
    config_file = tmp_path / "config.ini"
    config_file.write_text(text)
    monkeypatch.setattr(settings.config, "config_file", config_file)
    result = CliRunner().invoke(cli, ["config", "show"])
    assert result.exit_code == 0, result.output
    return result.output


def test_show_prints_no_secret_in_full(monkeypatch, tmp_path):
    out = _run_show(monkeypatch, tmp_path, CONFIG)
    for secret in (API_KEY, WORKSPACE_KEY, SESSION_TOKEN):
        assert secret not in out
    # the same shape `config get api.api_key` prints
    assert f"api_key = {API_KEY[:8]}...{API_KEY[-4:]}" in out
    assert f"api_key = {WORKSPACE_KEY[:8]}...{WORKSPACE_KEY[-4:]}" in out
    assert f"token = {SESSION_TOKEN[:8]}...{SESSION_TOKEN[-4:]}" in out


def test_show_keeps_everything_that_is_not_a_secret(monkeypatch, tmp_path):
    out = _run_show(monkeypatch, tmp_path, CONFIG)
    assert "key_path = /home/me/.ssh/lium.ed25519" in out
    assert "id = 7c1e" in out
    assert "# a comment that mentions token = keep-me" in out
    # rich wraps the long path line at the runner's 80 columns: compare without whitespace
    assert str(tmp_path / "config.ini") in re.sub(r"\s+", "", out)


def test_show_without_a_config_file_prints_only_the_path(monkeypatch, tmp_path):
    missing = tmp_path / "config.ini"
    monkeypatch.setattr(settings.config, "config_file", missing)
    result = CliRunner().invoke(cli, ["config", "show"])
    assert result.exit_code == 0, result.output
    assert re.sub(r"\s+", "", result.output) == f"#{missing}"


def test_mask_config_text_is_keyed_by_section():
    # `token` outside [session] is not the session token; `api_key` is a secret in every section
    text = "[other]\ntoken = plain-value-stays\n[custom]\napi_key: lium-test-custom-0123456789abcdef\n"
    masked = mask_config_text(text)
    assert "token = plain-value-stays" in masked
    assert "lium-test-custom-0123456789abcdef" not in masked
    assert "api_key: lium-tes...cdef" in masked


def test_mask_config_text_short_secret_is_starred():
    assert mask_config_text("[api]\napi_key = short\n") == "[api]\napi_key = ***\n"


def test_mask_config_text_reads_the_file_like_configparser():
    # lower-cased option names, a `[section] ; comment` header, a uniformly indented section, and a
    # continuation line (indented more than its option) joined into the value: what configparser does
    text = (
        "[api] ; main\n"
        "  url = http://x\n"
        "  API_KEY = lium-test-key-0123456789abcdefghij\n"
        "      tail-of-the-key-on-line-two\n"
        "  note = keep-me\n"
        "      second-note-line\n"
        "# copied here by hand: lium-test-key-0123456789abcdefghij\n"
    )
    masked = mask_config_text(text)
    assert "lium-test-key-0123456789abcdefghij" not in masked
    assert "tail-of-the-key-on-line-two" not in masked
    assert "  API_KEY = lium-tes...-two" in masked   # the last 4 of the joined value, as `config get` prints it
    assert "      ...\n" in masked
    assert "# copied here by hand: lium-tes...-two" in masked
    assert "  url = http://x" in masked and "      second-note-line" in masked


def test_mask_config_text_short_value_does_not_eat_other_words():
    text = "[api]\napi_key = root\n[ssh]\nkey_path = /home/root/.ssh/id_rooted\n"
    assert mask_config_text(text) == "[api]\napi_key = ***\n[ssh]\nkey_path = /home/root/.ssh/id_rooted\n"


def test_show_prints_nothing_but_a_note_when_the_file_does_not_parse(monkeypatch, tmp_path):
    out = _run_show(monkeypatch, tmp_path, "api_key = lium-test-key-0123456789abcdefghij\n[api\n")
    # rich wraps the path line at 80 columns; compare without whitespace
    assert re.sub(r"\s+", "", out) == re.sub(
        r"\s+", "", f"# {tmp_path / 'config.ini'}\n# not shown: the file does not parse as a config file; open it to read it"
    )


def test_mask_config_text_masks_the_default_section_and_refuses_duplicates():
    # [DEFAULT] values are read by ConfigParser too; a duplicate option is what the CLI's parser refuses
    assert mask_config_text("[DEFAULT]\napi_key = lium-test-key-0123456789abcdefghij\n") == (
        "[DEFAULT]\napi_key = lium-tes...ghij\n"
    )
    assert mask_config_text("[api]\napi_key = a-0123456789abcdefghij\napi_key = b-0123456789abcdefghij\n") is None
