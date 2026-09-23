"""DAH-1482: `lium secrets` and `Lium.secrets` — a value only ever travels on stdin or a hidden prompt and
through `encrypt_for_upload`; listings carry names and times, never values; with LIUM_SECRETS_ENABLED
unset the CLI and the rent payloads are exactly today's."""

import json
from types import SimpleNamespace

import pytest
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.secrets import command as secrets_command
from lium.cli.up import command as up_command
from lium.sdk import Config, Lium, LiumError
from lium.sdk import secrets as sdk_secrets
from lium.sdk.secrets import SecretInfo, encrypt_for_upload

BASE = "https://lium.io/api"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIUserKeyForTestingPurposesOnly user@test"
VALUE = "hf_SECRET_VALUE_MARKER"


class FakeSecrets:
    def __init__(self):
        self.set_calls = []
        self.deleted = []
        self.rows = [SecretInfo(name="HF_TOKEN", updated_at="2026-09-23T12:00:00Z")]

    def list(self):
        return list(self.rows)

    def set(self, name, value):
        self.set_calls.append((name, value))
        return SecretInfo(name=name)

    def delete(self, name):
        self.deleted.append(name)


@pytest.fixture
def fake(monkeypatch):
    secrets = FakeSecrets()
    monkeypatch.setattr(secrets_command, "Lium", lambda *a, **k: SimpleNamespace(secrets=secrets))
    monkeypatch.setattr(secrets_command, "ensure_config", lambda: None)
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    return secrets


def _run(args, **kwargs):
    return CliRunner().invoke(cli, args, catch_exceptions=False, **kwargs)


# --- CLI -------------------------------------------------------------------------------------


def test_set_takes_no_value_parameter_at_all():
    assert [p.name for p in secrets_command.secrets_set_command.params] == ["name"]


def test_set_reads_the_value_from_stdin(fake):
    result = _run(["secrets", "set", "HF_TOKEN"], input=f"{VALUE}\n")

    assert result.exit_code == 0, result.output
    assert fake.set_calls == [("HF_TOKEN", VALUE)]
    assert VALUE not in result.output


def test_set_keeps_inner_newlines_and_drops_only_the_last(fake):
    _run(["secrets", "set", "PEM"], input="line1\nline2\n")
    assert fake.set_calls == [("PEM", "line1\nline2")]


def test_set_prompts_hidden_and_twice_when_stdin_is_a_terminal(fake, monkeypatch):
    monkeypatch.setattr(secrets_command, "stdin_is_interactive", lambda: True)

    result = _run(["secrets", "set", "HF_TOKEN"], input=f"{VALUE}\n{VALUE}\n")

    assert result.exit_code == 0, result.output
    assert fake.set_calls == [("HF_TOKEN", VALUE)]
    assert "Value for HF_TOKEN" in result.output
    assert VALUE not in result.output


def test_a_value_on_argv_is_refused_without_echoing_it(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "HF_TOKEN", VALUE])

    assert result.exit_code == 2
    assert fake.set_calls == []
    assert "never taken from the command line" in result.output
    assert VALUE not in result.output


def test_an_empty_value_is_refused(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "HF_TOKEN"], input="\n")
    assert result.exit_code == 2
    assert fake.set_calls == []


def test_a_bad_name_is_refused_before_reading_anything(fake):
    result = CliRunner().invoke(cli, ["secrets", "set", "../etc/passwd"], input=VALUE)
    assert result.exit_code == 2
    assert fake.set_calls == []


def test_list_shows_names_and_times_never_values(fake):
    fake.rows = [SecretInfo(name="HF_TOKEN", updated_at="2026-09-23T12:00:00Z"), SecretInfo(name="WANDB_API_KEY")]

    table = _run(["secrets", "list"])
    as_json = _run(["secrets", "list", "--json"])

    assert "HF_TOKEN" in table.output and "WANDB_API_KEY" in table.output
    assert json.loads(as_json.output) == [
        {"name": "HF_TOKEN", "updated_at": "2026-09-23T12:00:00Z"},
        {"name": "WANDB_API_KEY", "updated_at": None},
    ]


def test_rm_deletes_after_yes(fake):
    result = _run(["secrets", "rm", "HF_TOKEN", "-y"])
    assert result.exit_code == 0, result.output
    assert fake.deleted == ["HF_TOKEN"]


@pytest.mark.parametrize("args", [["secrets", "list"], ["secrets", "rm", "HF_TOKEN", "-y"], ["secrets", "set", "HF_TOKEN"]])
def test_flag_off_refuses_every_subcommand(fake, monkeypatch, args):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED")

    result = CliRunner().invoke(cli, args, input=VALUE)

    assert result.exit_code == 2
    assert "LIUM_SECRETS_ENABLED=1" in result.output
    assert fake.set_calls == [] and fake.deleted == []


def test_flag_off_hides_the_group_and_up_secret():
    # hidden is fixed at import, and the suite runs with the flag unset
    assert secrets_command.secrets_command.hidden is True
    assert next(p for p in up_command.up_command.params if p.name == "secret_names").hidden is True
    assert "secrets" not in _run(["--help"]).output
    assert "--secret" not in _run(["up", "--help"]).output


def test_up_secret_with_the_flag_off_is_refused(monkeypatch):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED", raising=False)
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["up", "exec-1", "--secret", "HF_TOKEN", "-y"])

    assert result.exit_code == 2
    assert "LIUM_SECRETS_ENABLED=1" in result.output


# --- SDK -------------------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("LIUM_SECRETS_ENABLED", "1")
    monkeypatch.setattr(Lium, "_ensure_ssh_keys_registered", lambda self, keys, name=None: None)
    lium = Lium(Config(api_key="test"))
    monkeypatch.setattr(lium, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(lium, "_pod_ids_before_rent", lambda: set())
    return lium


@responses.activate
def test_sdk_set_sends_the_body_encrypt_for_upload_returns(client, monkeypatch):
    seen = []

    def fake_encrypt(name, value):
        seen.append((name, value))
        return {"ciphertext": "opaque", "encryption": "test-scheme"}

    monkeypatch.setattr(sdk_secrets, "encrypt_for_upload", fake_encrypt)
    responses.add(responses.PUT, f"{BASE}/secrets/HF_TOKEN", json={"name": "HF_TOKEN", "updated_at": "t"})

    info = client.secrets.set("HF_TOKEN", VALUE)

    assert seen == [("HF_TOKEN", VALUE)]
    assert json.loads(responses.calls[0].request.body) == {"ciphertext": "opaque", "encryption": "test-scheme"}
    assert info == SecretInfo(name="HF_TOKEN", updated_at="t")


def test_encrypt_for_upload_is_todays_no_op():
    assert encrypt_for_upload("HF_TOKEN", VALUE) == {"value": VALUE, "encryption": "none"}


@responses.activate
def test_sdk_list_keeps_names_and_times_even_if_a_server_sent_a_value(client):
    responses.add(responses.GET, f"{BASE}/secrets", json=[{"name": "HF_TOKEN", "updated_at": "t", "value": VALUE}])

    rows = client.secrets.list()

    assert rows == [SecretInfo(name="HF_TOKEN", updated_at="t")]
    assert VALUE not in repr(rows)


@responses.activate
def test_sdk_delete(client):
    responses.add(responses.DELETE, f"{BASE}/secrets/HF_TOKEN", status=204)
    client.secrets.delete("HF_TOKEN")
    assert responses.calls[0].request.method == "DELETE"


def test_sdk_refuses_with_the_flag_off(client, monkeypatch):
    monkeypatch.delenv("LIUM_SECRETS_ENABLED")
    with pytest.raises(LiumError, match="LIUM_SECRETS_ENABLED=1"):
        client.secrets.list()
    with pytest.raises(LiumError, match="LIUM_SECRETS_ENABLED=1"):
        client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN"])


@responses.activate
def test_up_sends_secret_names_and_never_a_value(client):
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})

    client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["HF_TOKEN", "WANDB", "HF_TOKEN"])

    sent = json.loads(responses.calls[-1].request.body)
    assert sent["secret_names"] == ["HF_TOKEN", "WANDB"]


@responses.activate
def test_up_without_secrets_sends_todays_payload(client):
    responses.add(responses.POST, f"{BASE}/executors/exec-1/rent", json={"id": "pod-1"})

    client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY])

    assert json.loads(responses.calls[-1].request.body) == {
        "pod_name": "Your Pod", "template_id": "tpl", "dockerfile_content": None, "volume_id": None,
        "user_public_key": [KEY], "initial_port_count": None, "enable_volume_encryption": True,
        "backup_log_id": None, "restore_path": None,
    }


def test_up_refuses_a_bad_secret_name_before_renting(client):
    with pytest.raises(ValueError):
        client.up(executor_id="exec-1", template_id="tpl", ssh_keys=[KEY], secret_names=["BAD-NAME"])
