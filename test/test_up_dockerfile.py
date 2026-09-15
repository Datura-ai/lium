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
    assert payload["enable_volume_encryption"] is True


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [
        (None, None),
        (False, False),
        (True, True),
    ],
)
def test_up_sends_volume_encryption_preference(monkeypatch, enabled, expected):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    client.up(
        executor_id="exec-1",
        ssh_keys=["ssh-ed25519 AAA"],
        enable_volume_encryption=enabled,
    )

    assert captured["payload"]["enable_volume_encryption"] is expected


def test_up_sends_startup_restore(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    client.up(
        executor_id="exec-1",
        ssh_keys=["ssh-ed25519 AAA"],
        backup_id="backup-123",
        restore_path="/root/restored",
    )

    assert captured["payload"]["backup_log_id"] == "backup-123"
    assert captured["payload"]["restore_path"] == "/root/restored"


@pytest.mark.parametrize(
    ("backup_id", "restore_path"),
    [("backup-123", None), (None, "/root/restored")],
)
def test_up_requires_complete_startup_restore_pair(monkeypatch, backup_id, restore_path):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    with pytest.raises(ValueError, match="must be provided together"):
        client.up(
            executor_id="exec-1",
            ssh_keys=["ssh-ed25519 AAA"],
            backup_id=backup_id,
            restore_path=restore_path,
        )

    assert "payload" not in captured


def test_ps_hydrates_volume_encryption_outcome(monkeypatch):
    client = Lium(Config(api_key="test"))
    response = SimpleNamespace(
        json=lambda: [
            {
                "id": "pod-1",
                "pod_name": "encrypted-pod",
                "status": "RUNNING",
                "enable_volume_encryption": True,
                "volume_encryption_status": "ENABLED",
            }
        ]
    )
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: response)

    pod = client.ps()[0]

    assert pod.enable_volume_encryption is True
    assert pod.volume_encryption_status == "ENABLED"


def test_up_rejects_both_template_and_dockerfile(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)

    # Act / Assert — XOR guard fires before any network call
    with pytest.raises(ValueError, match="only one of"):
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


def test_up_command_exposes_volume_encryption_flag():
    result = CliRunner().invoke(up_command.up_command, ["--help"])
    assert result.exit_code == 0
    assert "--volume-encryption" in result.output
    assert "--no-volume-encryption" in result.output


def test_up_command_exposes_startup_restore_flags():
    result = CliRunner().invoke(up_command.up_command, ["--help"])
    assert result.exit_code == 0
    assert "--restore-backup" in result.output
    assert "--restore-to" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["node-1", "--restore-backup", "backup-123"],
        ["node-1", "--restore-to", "/root/restored"],
    ],
)
def test_up_command_requires_complete_startup_restore_pair(monkeypatch, args):
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(up_command.up_command, args)

    assert result.exit_code == 2
    assert "--restore-backup and --restore-to must be provided together" in result.output


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
            "enable_volume_encryption": True,
        }
    )

    # Assert
    assert result.ok
    assert captured["dockerfile_content"] == 'FROM busybox\nCMD ["true"]'
    assert captured["template_id"] is None
    assert captured["enable_volume_encryption"] is True
    assert captured["executor_id"] == "exec-1"


def test_rent_pod_action_passes_startup_restore():
    captured: dict = {}

    class FakeLium:
        def up(self, **kwargs):
            captured.update(kwargs)
            return {"id": "pod-1", "name": kwargs["name"]}

    result = RentPodAction().execute(
        {
            "lium": FakeLium(),
            "executor": SimpleNamespace(id="exec-1", huid="brave-fox-3a"),
            "template": SimpleNamespace(id="template-1"),
            "name": "restored-pod",
            "backup_id": "backup-123",
            "restore_path": "/root/restored",
        }
    )

    assert result.ok
    assert captured["backup_id"] == "backup-123"
    assert captured["restore_path"] == "/root/restored"


# --------------------------------------------------------------------------- #
# CLI: full `lium up --dockerfile` path reads the file and forwards its content
# --------------------------------------------------------------------------- #


def test_up_command_reads_dockerfile_and_forwards_content(monkeypatch, tmp_path):
    # Arrange — a real Dockerfile on disk
    dockerfile = tmp_path / "Dockerfile"
    dockerfile_text = 'FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04\nCMD ["sleep", "infinity"]\n'
    dockerfile.write_text(dockerfile_text)

    captured: dict = {}
    short_backup_id = "8fbb30f6"
    full_backup_id = "8fbb30f6-6026-4043-98c7-c4189dc09bef"
    executor = SimpleNamespace(
        id="exec-1",
        huid="brave-fox-3a",
        gpu_count=1,
        gpu_type="A6000",
        price_per_hour=0.24,
        available_port_count=10,
        download_speed=1000,
    )
    pod = SimpleNamespace(id="pod-1", name="custom-pod", huid="brave-fox-3a")

    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)  # a server without workspaces: `up` reads it for its workspace line

        def resolve_backup_id(self, backup_id):
            assert backup_id == short_backup_id
            return full_backup_id

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
            return ActionResult(ok=True, data={"ssh_argv": ["ssh", "-p", "22", "root@203.0.113.7"], "pod": pod})

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: _FakeLium())
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _FakeResolveExecutor)
    monkeypatch.setattr(up_command, "RentPodAction", _FakeRentPod)
    monkeypatch.setattr(up_command, "WaitReadyAction", _FakeWaitReady)
    monkeypatch.setattr(up_command, "PrepareSSHAction", _FakePrepareSSH)
    monkeypatch.setattr("lium.cli.ssh.command.ssh_session_connected", lambda *a, **k: True)

    # Act
    result = CliRunner().invoke(
        up_command.up_command,
        [
            "brave-fox-3a",
            "--dockerfile",
            str(dockerfile),
            "--restore-backup",
            short_backup_id,
            "--restore-to",
            "/root/restored",
            "--yes",
        ],
    )

    # Assert
    assert result.exit_code == 0, result.output
    assert "ctx" in captured, "RentPodAction was never reached"
    assert captured["ctx"]["dockerfile_content"] == dockerfile_text
    assert captured["ctx"]["template"] is None
    assert captured["ctx"]["backup_id"] == full_backup_id
    assert captured["ctx"]["restore_path"] == "/root/restored"
    assert "Restore is continuing in /root/restored" in result.output
    assert "Do not modify that directory" in result.output

def test_up_command_rejects_dockerfile_with_image(monkeypatch, tmp_path):
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM busybox\n")
    monkeypatch.setattr(up_command, "ensure_config", lambda: None)

    result = CliRunner().invoke(
        up_command.up_command,
        ["brave-fox-3a", "--dockerfile", str(dockerfile), "--image", "pytorch/pytorch:2.0"],
    )

    # A rejected invocation must not report success — DAH-2556.
    assert result.exit_code == 2
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

    assert result.exit_code == 2
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

    assert result.exit_code == 2
    assert "Could not read Dockerfile" in result.output


# --------------------------------------------------------------------------- #
# SDK: Lium.up(image=...) — docker image only, defaults for the rest (DAH-2103)
# --------------------------------------------------------------------------- #


def test_up_with_image_creates_a_one_time_template_and_rents_it(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    created: dict = {}

    def fake_create_template(**kwargs):
        created.update(kwargs)
        return SimpleNamespace(id="tmpl-ephemeral")

    monkeypatch.setattr(client, "create_template", fake_create_template)

    # Act
    client.up(executor_id="exec-1", image="pytorch/pytorch:2.9.1-cuda13.0-cudnn9-runtime", ssh_keys=["ssh-ed25519 AAA"])

    # Assert — the same template `lium up --image` creates, then the normal rent.
    # The reference goes to the backend whole; it splits name, tag and digest
    # (parse_image_reference_parts, DAH-2739) — no client-side rsplit(":").
    assert created["docker_image"] == "pytorch/pytorch:2.9.1-cuda13.0-cudnn9-runtime"
    assert created["docker_image_tag"] == ""
    assert created["ports"] == [22]
    assert created["is_private"] is True and created["one_time_template"] is True
    assert captured["payload"]["template_id"] == "tmpl-ephemeral"
    assert captured["payload"]["dockerfile_content"] is None


@pytest.mark.parametrize(
    "image",
    [
        "ubuntu",  # no tag: the backend defaults it to latest
        "registry.example.com:5000/team/img",  # a colon before the last '/' is a registry port
        "repo/name@sha256:" + "a" * 64,  # a digest reference
    ],
)
def test_up_with_image_hands_the_reference_over_whole(monkeypatch, image):
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    _stub_up(monkeypatch, client, captured)
    created: dict = {}
    monkeypatch.setattr(
        client, "create_template", lambda **kwargs: created.update(kwargs) or SimpleNamespace(id="t")
    )

    client.up(executor_id="exec-1", image=image, ssh_keys=["ssh-ed25519 AAA"])

    assert (created["docker_image"], created["docker_image_tag"]) == (image, "")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"image": "ubuntu", "template_id": "tmpl-xyz"},
        {"image": "ubuntu", "dockerfile_content": "FROM busybox"},
    ],
)
def test_up_rejects_image_combined_with_another_source(monkeypatch, kwargs):
    client = Lium(Config(api_key="test"))
    _stub_up(monkeypatch, client, {})

    with pytest.raises(ValueError, match="only one of"):
        client.up(executor_id="exec-1", ssh_keys=["ssh-ed25519 AAA"], **kwargs)
