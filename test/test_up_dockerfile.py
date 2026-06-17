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

from lium.sdk import Config, Lium
from lium.cli import up as up_pkg  # noqa: F401  (ensures package import works)
from lium.cli.up import command as up_command
from lium.cli.up import validation as up_validation
from lium.cli.up.actions import RentPodAction
from lium.cli.actions import ActionResult


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def _stub_up(monkeypatch, client, captured):
    """Stub out network + ssh helpers on a real Lium so up() reaches payload build."""
    monkeypatch.setattr(client, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(
        client, "default_docker_template", lambda executor_id: SimpleNamespace(id="tmpl-default")
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


def test_up_with_dockerfile_sends_content_and_null_template(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    dockerfile = 'FROM busybox\nCMD ["true"]'

    # Act
    client.up(
        executor_id="exec-1",
        name="custom-pod",
        dockerfile_content=dockerfile,
        ssh_keys=["ssh-ed25519 AAA"],
    )

    # Assert
    assert captured["endpoint"] == "/executors/exec-1/rent"
    payload = captured["payload"]
    assert payload["dockerfile_content"] == dockerfile
    assert payload["template_id"] is None


def test_up_with_template_sends_template_and_null_dockerfile(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    # Act
    client.up(executor_id="exec-1", template_id="tmpl-xyz", ssh_keys=["ssh-ed25519 AAA"])

    # Assert
    payload = captured["payload"]
    assert payload["template_id"] == "tmpl-xyz"
    assert payload["dockerfile_content"] is None


def test_up_without_template_or_dockerfile_falls_back_to_default_template(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    # Act
    client.up(executor_id="exec-1", ssh_keys=["ssh-ed25519 AAA"])

    # Assert
    payload = captured["payload"]
    assert payload["template_id"] == "tmpl-default"
    assert payload["dockerfile_content"] is None


def test_up_rejects_both_template_and_dockerfile(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    # Act / Assert — XOR guard fires before any network call
    with pytest.raises(ValueError, match="not both"):
        client.up(
            executor_id="exec-1",
            template_id="tmpl-xyz",
            dockerfile_content="FROM busybox",
            ssh_keys=["ssh-ed25519 AAA"],
        )
    assert "payload" not in captured


# --------------------------------------------------------------------------- #
# CLI: validation XOR
# --------------------------------------------------------------------------- #


def test_validate_rejects_dockerfile_with_image():
    ok, error = up_validation.validate(
        "node-1", None, None, None, None, None, image="pytorch/pytorch", dockerfile="./Dockerfile"
    )
    assert ok is False
    assert "--dockerfile" in error and "--image" in error


def test_validate_rejects_dockerfile_with_template_id():
    ok, error = up_validation.validate(
        "node-1", None, None, None, None, None, template_id="tmpl-1", dockerfile="./Dockerfile"
    )
    assert ok is False
    assert "--dockerfile" in error and "--template_id" in error


def test_validate_allows_dockerfile_alone():
    ok, error = up_validation.validate(
        "node-1", None, None, None, None, None, dockerfile="./Dockerfile"
    )
    assert ok is True
    assert error == ""


# --------------------------------------------------------------------------- #
# CLI: RentPodAction threads dockerfile_content into the SDK
# --------------------------------------------------------------------------- #


def test_rent_pod_action_passes_dockerfile_content_and_no_template():
    # Arrange
    captured: dict = {}

    class FakeLium:
        def up(self, **kwargs):
            captured.update(kwargs)
            return {"id": "pod-1", "name": kwargs["name"]}

    # Act
    result = RentPodAction().execute(
        {
            "lium": FakeLium(),
            "executor": SimpleNamespace(id="exec-1", huid="brave-fox-3a"),
            "template": None,
            "dockerfile_content": "FROM busybox\nCMD [\"true\"]",
            "name": "custom-pod",
        }
    )

    # Assert
    assert result.ok
    assert captured["dockerfile_content"] == 'FROM busybox\nCMD ["true"]'
    assert captured["template_id"] is None
    assert captured["executor_id"] == "exec-1"


# --------------------------------------------------------------------------- #
# CLI: full `lium up --dockerfile` path reads the file and forwards its content
# --------------------------------------------------------------------------- #


def test_up_command_reads_dockerfile_and_forwards_content(monkeypatch, tmp_path):
    # Arrange — a real Dockerfile on disk
    dockerfile = tmp_path / "Dockerfile"
    dockerfile_text = 'FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04\nCMD ["sleep", "infinity"]\n'
    dockerfile.write_text(dockerfile_text)

    captured: dict = {}
    executor = SimpleNamespace(
        id="exec-1",
        huid="brave-fox-3a",
        gpu_count=1,
        gpu_type="A6000",
        price_per_hour=0.24,
        available_port_count=10,
        download_speed=1000,
    )
    pod = SimpleNamespace(id="pod-1", name="custom-pod")

    class _FakeResolveExecutor:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _FakeRentPod:
        def execute(self, ctx):
            captured["ctx"] = ctx
            return ActionResult(ok=True, data={"pod_info": {}, "pod_id": "pod-1", "pod_name": "custom-pod"})

    class _FakeWaitReady:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"pod": pod})

    class _FakePrepareSSH:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"ssh_cmd": "ssh root@host -p 22", "pod": pod})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _FakeResolveExecutor)
    monkeypatch.setattr(up_command, "RentPodAction", _FakeRentPod)
    monkeypatch.setattr(up_command, "WaitReadyAction", _FakeWaitReady)
    monkeypatch.setattr(up_command, "PrepareSSHAction", _FakePrepareSSH)
    monkeypatch.setattr("lium.cli.ssh.command.ssh_to_pod", lambda *a, **k: None)

    # Act
    result = CliRunner().invoke(
        up_command.up_command,
        ["brave-fox-3a", "--dockerfile", str(dockerfile), "--yes"],
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert "ctx" in captured, "RentPodAction was never reached"
    assert captured["ctx"]["dockerfile_content"] == dockerfile_text
    assert captured["ctx"]["template"] is None


def test_up_command_rejects_dockerfile_with_image(monkeypatch, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM busybox\n")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(
        up_command.up_command,
        ["brave-fox-3a", "--dockerfile", str(dockerfile), "--image", "pytorch/pytorch:2.0"],
    )

    assert result.exit_code == 0
    assert "Cannot specify both --dockerfile and --image" in result.output


def test_up_command_rejects_env_with_dockerfile(monkeypatch, tmp_path):
    # --env/--cmd/--entrypoint/--internal-ports have no delivery path in a custom
    # build, so they must be rejected rather than silently ignored.
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM busybox\n")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(
        up_command.up_command,
        ["brave-fox-3a", "--dockerfile", str(dockerfile), "-e", "KEY=VAL"],
    )

    assert result.exit_code == 0
    assert "--env" in result.output
    assert "--dockerfile" in result.output


def test_up_command_reports_non_utf8_dockerfile(monkeypatch, tmp_path):
    # A binary / non-UTF-8 file passes click's readable check but must yield a
    # clean error, not an uncaught UnicodeDecodeError.
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_bytes(b"\xff\xfe\x00\x01binary-not-text")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(
        up_command.up_command,
        ["brave-fox-3a", "--dockerfile", str(dockerfile)],
    )

    assert result.exit_code == 0
    assert "Could not read Dockerfile" in result.output
