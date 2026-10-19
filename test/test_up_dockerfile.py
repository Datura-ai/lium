"""Tests for custom-Dockerfile pod deployment via `lium up --dockerfile`.

Covers the three surfaces the feature touches:
  * SDK  — `Lium.up()` payload shape (dockerfile vs template) + the XOR guard.
  * CLI  — `--dockerfile` validation (mutually exclusive with --image/--template_id)
           and the file-read → SDK wiring.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.sdk import Config, Lium, LiumError
from lium.cli import up as up_pkg  # noqa: F401  (ensures package import works)
from lium.cli.up import command as up_command


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _stub_up(monkeypatch, client, captured):
    """Stub out network + ssh helpers on a real Lium so up() reaches payload build."""
    monkeypatch.setattr(client, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(
        client,
        "default_docker_template",
        lambda executor_id: SimpleNamespace(id="tmpl-default", supports_volume_encryption=True),
    )
    monkeypatch.setattr(client, "_ensure_ssh_keys_registered", lambda *a, **k: None)

    def fake_request(method, endpoint, json=None, **kwargs):
        captured["method"] = method
        captured["endpoint"] = endpoint
        captured["payload"] = json
        return _Resp({"id": "pod-1", "name": (json or {}).get("pod_name"), "status": "PENDING"})

    monkeypatch.setattr(client, "_request", fake_request)


# --------------------------------------------------------------------------- #
# SDK: Lium.up() payload shape
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("supported", "expected"), [(True, True), (False, False)])
def test_up_requests_encryption_only_when_the_template_supports_it(monkeypatch, supported, expected):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    monkeypatch.setattr(
        client, "get_template", lambda template_id: SimpleNamespace(supports_volume_encryption=supported)
    )

    client.up(executor_id="exec-1", template_id="11111111-1111-4111-8111-111111111111", ssh_keys=["ssh-ed25519 AAA"])

    assert captured["payload"]["enable_volume_encryption"] is expected


@pytest.mark.parametrize(("entry", "server_side"), [("up", False), ("rent", False), ("rent", True)])
def test_a_failed_template_lookup_refuses_to_rent_rather_than_drop_encryption(monkeypatch, entry, server_side):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    monkeypatch.setattr(client, "get_template", lambda template_id: None)  # the GET failed
    monkeypatch.setattr(client, "supports", lambda feature: server_side)
    target = {"executor_id": "exec-1"} if entry == "up" else {"gpu_type": "H100"}

    with pytest.raises(LiumError, match="Could not look up template 11111111-1111-4111-8111-111111111111"):
        getattr(client, entry)(**target, template_id="11111111-1111-4111-8111-111111111111", ssh_keys=["ssh-ed25519 AAA"])

    assert not captured  # nothing was rented


def test_up_with_dockerfile_does_not_request_encryption(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    client.up(executor_id="exec-1", dockerfile_content="FROM busybox", ssh_keys=["ssh-ed25519 AAA"])

    assert captured["payload"]["enable_volume_encryption"] is False


def test_up_requests_encryption_when_asked_on_a_template_without_support(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    monkeypatch.setattr(
        client, "get_template", lambda template_id: SimpleNamespace(supports_volume_encryption=False)
    )

    client.up(
        executor_id="exec-1", template_id="11111111-1111-4111-8111-111111111111", ssh_keys=["ssh-ed25519 AAA"], enable_volume_encryption=True
    )

    assert captured["payload"]["enable_volume_encryption"] is True


def test_get_template_reads_volume_encryption_support(monkeypatch):
    client = Lium(Config(api_key="test"))
    monkeypatch.setattr(
        client,
        "_request",
        lambda method, endpoint, **kwargs: _Resp({"id": "22222222-2222-4222-8222-222222222222", "supports_volume_encryption": True}),
    )

    template = client.get_template("22222222-2222-4222-8222-222222222222")

    assert template.supports_volume_encryption is True


def test_up_command_leaves_volume_encryption_to_the_template_by_default():
    (option,) = [p for p in up_command.up_command.params if p.name == "volume_encryption"]
    assert option.default is None


# --------------------------------------------------------------------------- #
# CLI: validation XOR
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# CLI: RentPodAction threads dockerfile_content into the SDK
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# CLI: full `lium up --dockerfile` path reads the file and forwards its content
# --------------------------------------------------------------------------- #


def test_up_command_refuses_copy_of_local_files_before_renting(monkeypatch, tmp_path):
    # Arrange: a backend without build-context uploads builds with the Dockerfile as the
    # only file, so COPY of a file next to it would fail there after the rent.
    (tmp_path / "app.py").write_text("print('hi')\n")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nCOPY app.py /app/app.py\nCMD [\"python\", \"/app/app.py\"]\n")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    lium = SimpleNamespace(
        supports=lambda feature: False,
        upload_build_context=lambda archive, timeout: pytest.fail("uploaded"),
    )
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: lium)
    monkeypatch.setattr(up_command, "ResolveExecutorAction", lambda: pytest.fail("rent path reached"))

    # Act
    result = CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--dockerfile", str(dockerfile)])

    # Assert
    assert result.exit_code == 2
    assert "COPY app.py /app/app.py" in result.output
    assert "--image" in result.output


# --------------------------------------------------------------------------- #
# SDK: Lium.up(image=...) — docker image only, defaults for the rest (DAH-2103)
# --------------------------------------------------------------------------- #


