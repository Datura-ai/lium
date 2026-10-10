"""`lium up --dockerfile` with COPY/ADD of local files: the Dockerfile's directory goes up as the build context."""

from __future__ import annotations

import gzip
import io
import os
import tarfile
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.actions import ActionResult
from lium.cli.up import command as up_command
from lium.cli.up.actions import RentPodAction
from lium.sdk import Config, Lium
from lium.sdk import build_context
from lium.sdk.client import BUILD_CONTEXT


def _names(archive: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return sorted(member.name for member in tar.getmembers())


def _tree(root, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_pack_leaves_out_what_dockerignore_names(tmp_path):
    # Arrange
    _tree(
        tmp_path,
        {
            "Dockerfile": "FROM alpine\n",
            "app.py": "x",
            "src/lib.py": "x",
            "src/lib.pyc": "x",
            "deep/a/b/c.pyc": "x",
            ".git/HEAD": "x",
            "data/big.bin": "x",
            "data/keep.txt": "x",
            ".dockerignore": "# comment\n.git\n**/*.pyc\ndata\n!data/keep.txt\n/app.py\n",
        },
    )

    # Act
    archive = build_context.pack(tmp_path)

    # Assert
    assert _names(archive) == [
        ".dockerignore",
        "Dockerfile",
        "data/keep.txt",
        "deep",
        "deep/a",
        "deep/a/b",
        "src",
        "src/lib.py",
    ]


def test_pack_drops_local_owners(tmp_path):
    # Arrange
    _tree(tmp_path, {"app.py": "x"})

    # Act
    archive = build_context.pack(tmp_path)

    # Assert
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember("app.py")
    assert (member.uid, member.gid, member.uname, member.gname) == (0, 0, "", "")


def test_pack_is_the_same_bytes_for_the_same_tree(tmp_path):
    # Arrange
    _tree(tmp_path, {"app.py": "x", "src/lib.py": "y"})

    # Act / Assert
    assert build_context.pack(tmp_path) == build_context.pack(tmp_path)


def test_pack_keeps_a_symlink_inside_the_context(tmp_path):
    # Arrange
    _tree(tmp_path, {"src/lib.py": "x"})
    os.symlink("src/lib.py", tmp_path / "lib.py")

    # Act
    archive = build_context.pack(tmp_path)

    # Assert
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert tar.getmember("lib.py").issym()


@pytest.mark.parametrize("target", ["/etc/passwd", "../outside"])
def test_pack_refuses_a_symlink_out_of_the_context(tmp_path, target):
    # Arrange
    os.symlink(target, tmp_path / "link")

    # Act / Assert
    with pytest.raises(ValueError, match="outside the build context"):
        build_context.pack(tmp_path)


def test_pack_refuses_more_than_max_bytes(tmp_path):
    # Arrange
    (tmp_path / "noise.bin").write_bytes(os.urandom(64 * 1024))

    # Act / Assert
    with pytest.raises(ValueError, match="larger than 1024 bytes"):
        build_context.pack(tmp_path, max_bytes=1024)


def test_upload_build_context_posts_the_archive_and_returns_its_sha256(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}

    def fake_request(method, endpoint, **kwargs):
        captured.update(method=method, endpoint=endpoint, **kwargs)
        return SimpleNamespace(json=lambda: {"sha256": "ab" * 32, "size_bytes": 3})

    monkeypatch.setattr(client, "_request", fake_request)

    # Act
    sha256 = client.upload_build_context(b"\x1f\x8bx")

    # Assert
    assert sha256 == "ab" * 32
    assert (captured["method"], captured["endpoint"], captured["data"]) == ("POST", "/build-contexts", b"\x1f\x8bx")
    assert captured["headers"]["Content-Type"] == "application/gzip"


def test_up_sends_the_build_context_sha256(monkeypatch):
    # Arrange
    client = Lium(Config(api_key="test"))
    captured: dict = {}
    monkeypatch.setattr(client, "get_executor", lambda executor_id: SimpleNamespace(id="exec-1"))
    monkeypatch.setattr(client, "_ensure_ssh_keys_registered", lambda *a, **k: None)
    monkeypatch.setattr(client, "_pod_ids_before_rent", lambda: frozenset())

    def fake_request(method, endpoint, json=None, **kwargs):
        captured["payload"] = json
        return SimpleNamespace(json=lambda: {"id": "pod-1", "status": "PENDING"})

    monkeypatch.setattr(client, "_request", fake_request)

    # Act
    client.up(
        executor_id="exec-1",
        dockerfile_content="FROM alpine\nCOPY app.py /app.py\n",
        build_context_sha256="ab" * 32,
        ssh_keys=["ssh-ed25519 AAA"],
    )

    # Assert
    assert captured["payload"]["build_context_sha256"] == "ab" * 32
    assert captured["payload"]["template_id"] is None


def test_rent_pod_action_passes_the_build_context_to_up():
    # Arrange
    calls: dict = {}
    lium = SimpleNamespace(up=lambda **kwargs: calls.update(kwargs) or {"id": "pod-1"})
    executor = SimpleNamespace(id="exec-1", huid="brave-fox-3a", price_per_hour=1.0, gpu_count=1)

    # Act
    result = RentPodAction().execute(
        {
            "lium": lium,
            "executor": executor,
            "dockerfile_content": "FROM alpine\n",
            "build_context_sha256": "ab" * 32,
        }
    )

    # Assert
    assert result.ok
    assert calls["build_context_sha256"] == "ab" * 32


def test_up_command_uploads_the_dockerfiles_directory_when_the_backend_takes_it(monkeypatch, tmp_path):
    # Arrange
    _tree(tmp_path, {"app.py": "print('hi')\n", "secrets.env": "x", ".dockerignore": "*.env\n"})
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nCOPY app.py /app/app.py\n")
    uploads: list[bytes] = []
    captured: dict = {}
    lium = SimpleNamespace(
        supports=lambda feature: feature == BUILD_CONTEXT,
        upload_build_context=lambda archive: uploads.append(archive) or "ab" * 32,
        workspaces=SimpleNamespace(current=lambda: None),
    )
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000", price_per_hour=0.24,
        available_port_count=10, download_speed=1000,
    )

    class _FakeResolveExecutor:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _FakeRentPod:
        def execute(self, ctx):
            captured["ctx"] = ctx
            raise SystemExit(0)  # stop here: what was sent is the assertion

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: lium)
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _FakeResolveExecutor)
    monkeypatch.setattr(up_command, "RentPodAction", _FakeRentPod)

    # Act
    CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--dockerfile", str(dockerfile), "--yes"])

    # Assert
    assert captured["ctx"]["build_context_sha256"] == "ab" * 32
    assert len(uploads) == 1
    assert _names(uploads[0]) == [".dockerignore", "Dockerfile", "app.py"]
    assert gzip.decompress(uploads[0])  # a gzipped tar, as the API takes it


def test_up_command_uploads_nothing_for_a_dockerfile_without_local_files(monkeypatch, tmp_path):
    # Arrange
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nRUN pip install requests\n")
    captured: dict = {}
    lium = SimpleNamespace(
        supports=lambda feature: True,
        upload_build_context=lambda archive: pytest.fail("uploaded"),
        workspaces=SimpleNamespace(current=lambda: None),
    )
    executor = SimpleNamespace(
        id="exec-1", huid="brave-fox-3a", gpu_count=1, gpu_type="A6000", price_per_hour=0.24,
        available_port_count=10, download_speed=1000,
    )

    class _FakeResolveExecutor:
        def execute(self, ctx):
            return ActionResult(ok=True, data={"executor": executor})

    class _FakeRentPod:
        def execute(self, ctx):
            captured["ctx"] = ctx
            raise SystemExit(0)

    monkeypatch.setattr(up_command, "ensure_config", lambda: None)
    monkeypatch.setattr(up_command, "Lium", lambda **kwargs: lium)
    monkeypatch.setattr(up_command, "ResolveExecutorAction", _FakeResolveExecutor)
    monkeypatch.setattr(up_command, "RentPodAction", _FakeRentPod)

    # Act
    CliRunner().invoke(up_command.up_command, ["brave-fox-3a", "--dockerfile", str(dockerfile), "--yes"])

    # Assert
    assert captured["ctx"]["build_context_sha256"] is None
