"""`lium whoami`: the first command to run when something is off.

Auth and balance errors already name the key they were raised for; `whoami`
answers the question before the error happens.
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.whoami import identity as identity_module
from lium.cli.whoami.identity import collect_identity
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED
from lium.sdk import LiumAuthError, LiumPermissionError, SSHKey

KEY = "sk_abcdef0123456789wxyz"
PUB = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample"


@pytest.fixture
def local_setup(monkeypatch, tmp_path):
    """A key in the environment, an ssh key pair under a temp HOME, ssh and rsync installed."""
    monkeypatch.setenv("LIUM_API_KEY", KEY)
    monkeypatch.delenv("LIUM_API_API_KEY", raising=False)  # it outranks LIUM_API_KEY since DAH-2896
    key = tmp_path / "id_ed25519"
    key.write_text("private")
    key.with_suffix(".pub").write_text(f"{PUB} user@host\n")
    monkeypatch.setattr(identity_module, "local_ssh_key_path", lambda: key)
    monkeypatch.setattr(identity_module.shutil, "which", lambda name: f"/usr/bin/{name}")
    return key


class _FakeLium:
    me_payload = {"id": "acct-1", "email": "user@example.com", "balance": 12.5}
    registered = [SSHKey(id="k1", name="laptop", public_key=f"{PUB} other-comment")]
    me_error = None

    def __init__(self, config=None, *a, **k):
        self.config = config or SimpleNamespace(base_url="https://api.example")
        self.config.base_url = getattr(self.config, "base_url", None) or "https://api.example"

    def me(self):
        if self.me_error:
            raise self.me_error
        return dict(self.me_payload)

    def list_ssh_keys(self):
        return list(self.registered)


@pytest.fixture
def fake_lium(monkeypatch):
    class _Lium(_FakeLium):
        pass

    monkeypatch.setattr(identity_module, "Lium", _Lium)
    return _Lium


# --- identity ------------------------------------------------------------------------------

def test_collect_identity_gathers_everything(local_setup, fake_lium):
    identity = collect_identity()

    assert identity.api_key_fingerprint == "sk_abc…wxyz"
    assert identity.api_key_source == "env:LIUM_API_KEY"
    assert identity.api_reachable is True and identity.api_latency_ms is not None
    assert identity.account_id == "acct-1" and identity.email == "user@example.com"
    assert identity.balance_usd == 12.5
    assert identity.ssh_key_path == str(local_setup)
    assert identity.ssh_public_key_found is True and identity.ssh_key_registered is True
    assert identity.ssh_client == "/usr/bin/ssh" and identity.rsync_client == "/usr/bin/rsync"


def test_collect_identity_follows_lium_base_url(monkeypatch, local_setup, fake_lium):
    monkeypatch.setenv("LIUM_BASE_URL", "http://localhost:8000/api")

    identity = collect_identity()

    assert identity.api_base_url == "http://localhost:8000/api"
    assert identity.ssh_key_path == str(local_setup)


def test_collect_identity_names_the_workspace_key_the_commands_run_with(monkeypatch, tmp_path, local_setup, fake_lium):
    """With a stored default (`lium workspaces use`), every command runs with `[workspace.<name>] api_key`;
    whoami must name that key, not `[api] api_key`, or its row contradicts `lium balance` and the auth errors."""
    monkeypatch.delenv("LIUM_API_KEY")
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".lium").mkdir()
    (tmp_path / ".lium" / "config.ini").write_text(
        "[api]\napi_key = sk_default_key_0000\n[workspaces]\nactive = research\n"
        "[workspace.research]\nid = ws-1\napi_key = sk_research_key_9999\n"
    )

    identity = collect_identity()

    assert identity.api_key_fingerprint == "sk_res…9999"
    assert identity.api_key_source.endswith("[workspace.research] api_key")


def test_collect_identity_without_a_key_stops_before_the_network(monkeypatch, local_setup, fake_lium):
    monkeypatch.delenv("LIUM_API_KEY")
    monkeypatch.setattr(identity_module, "resolve_api_key", lambda: (None, None))

    identity = collect_identity()

    assert identity.api_key_fingerprint == "none" and not identity.has_api_key
    assert identity.api_reachable is None and identity.account_id is None
    assert any("LIUM_API_KEY" in w for w in identity.warnings)


def test_collect_identity_keeps_an_api_error_instead_of_raising(local_setup, fake_lium):
    """A 401 is an answer: the API is reachable, the error is kept whole for the command to raise."""
    fake_lium.me_error = LiumAuthError("Invalid API key (key sk_abc…wxyz from env:LIUM_API_KEY)")

    identity = collect_identity()

    assert identity.api_reachable is True
    assert "Invalid API key" in identity.api_error
    assert identity.api_exception is fake_lium.me_error
    assert "api_exception" not in identity.to_dict()
    assert identity.account_id is None and identity.ssh_key_registered is None


def test_collect_identity_reports_a_network_failure_as_unreachable(local_setup, fake_lium):
    fake_lium.me_error = ConnectionError("dns: api.example")

    identity = collect_identity()

    assert identity.api_reachable is False
    assert identity.api_error == "ConnectionError: dns: api.example"
    assert identity.api_exception is None


def test_ssh_key_registration_compares_key_material_not_comments(local_setup, fake_lium):
    fake_lium.registered = [SSHKey(id="k", name="n", public_key="ssh-ed25519 AAAADifferent me@here")]

    assert collect_identity().ssh_key_registered is False


def test_public_key_material_ignores_missing_or_odd_files(tmp_path):
    assert identity_module.public_key_material(None) is None
    assert identity_module.public_key_material(tmp_path / "nope") is None
    key = tmp_path / "id_rsa"
    key.with_suffix(".pub").write_text("# just a comment\n")
    assert identity_module.public_key_material(key) is None


def test_local_ssh_key_path_prefers_the_configured_one(monkeypatch, tmp_path):
    from lium.cli import settings

    monkeypatch.setattr(settings.config, "get", lambda key: "~/keys/custom" if key == "ssh.key_path" else None)

    assert identity_module.local_ssh_key_path() == Path("~/keys/custom").expanduser()


# --- whoami --------------------------------------------------------------------------------

def test_whoami_prints_the_identity(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["whoami"])

    assert result.exit_code == 0, result.output
    assert "sk_abc…wxyz" in result.output and "env:LIUM_API_KEY" in result.output
    assert "acct-1" in result.output and "$12.50" in result.output
    assert "registered: yes" in result.output
    assert "reachable" in result.output


def test_whoami_json_is_the_identity_dict(local_setup, fake_lium):
    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["account_id"] == "acct-1"
    assert payload["api_key_fingerprint"] == "sk_abc…wxyz"
    assert payload["ssh_key_registered"] is True
    assert KEY not in result.output  # never the full key


def test_whoami_without_a_key_exits_configuration_error(monkeypatch, local_setup, fake_lium):
    """`--json` on a failure: stdout empty, the envelope on stderr carries the identity as `data`."""
    monkeypatch.setattr(identity_module, "resolve_api_key", lambda: (None, None))

    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert result.stdout == ""
    envelope = json.loads(result.stderr)
    assert envelope["ok"] is False and envelope["error"]["code"] == "no_api_key"
    assert envelope["data"]["api_key_fingerprint"] == "none"


def test_whoami_fails_like_every_other_command_on_a_401(local_setup, fake_lium):
    fake_lium.me_error = LiumAuthError("Invalid API key")

    result = CliRunner().invoke(cli, ["whoami"])

    assert result.exit_code == EXIT_API_ERROR
    assert "answered with an error: Invalid API key" in result.output
    assert "unreachable" not in result.output


def test_whoami_json_on_a_403_is_permission_denied_with_the_identity(local_setup, fake_lium):
    fake_lium.me_error = LiumPermissionError("Account not verified")

    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert result.stdout == ""
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "permission_denied"
    assert envelope["data"]["api_key_fingerprint"] == "sk_abc…wxyz"
    assert envelope["data"]["api_reachable"] is True


def test_whoami_with_an_unreachable_api_exits_api_error(local_setup, fake_lium):
    fake_lium.me_error = ConnectionError("dns: api.example")

    result = CliRunner().invoke(cli, ["whoami", "--json"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "api_unreachable"
    assert envelope["data"]["api_reachable"] is False
