"""`lium up --dockerfile` with COPY/ADD of local files: the Dockerfile's directory goes up as the build context."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
from types import SimpleNamespace

import pytest
import requests
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


def test_pack_prefers_the_dockerfiles_own_ignore_file(tmp_path):
    # Arrange: as with BuildKit, Dockerfile.prod.dockerignore replaces .dockerignore for that Dockerfile
    _tree(
        tmp_path,
        {
            "app.py": "x",
            "secrets.env": "x",
            "notes.md": "x",
            ".dockerignore": "*.md\n",
            "Dockerfile.prod.dockerignore": "*.env\n",
        },
    )

    # Act
    plain = _names(build_context.pack(tmp_path, dockerfile=tmp_path / "Dockerfile"))
    prod = _names(build_context.pack(tmp_path, dockerfile=tmp_path / "Dockerfile.prod"))

    # Assert
    assert "secrets.env" in plain and "notes.md" not in plain
    assert "secrets.env" not in prod and "notes.md" in prod


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

    first = build_context.pack(tmp_path)
    os.utime(tmp_path / "app.py", (1720000000, 1720000000))

    # Act / Assert
    assert build_context.pack(tmp_path) == first


@pytest.mark.parametrize(
    "pattern, ignored",
    [("*.env", ["secrets.env"]), ("**/*.env", ["secrets.env", "services/api/secrets.env"])],
)
def test_a_slashless_pattern_is_rooted_as_in_docker(tmp_path, pattern, ignored):
    # Arrange: Docker matches `*.env` from the context root only; `**/*.env` reaches every depth
    _tree(tmp_path, {".dockerignore": pattern + "\n"})
    rules = build_context.read_dockerignore(tmp_path)

    # Act
    result = [p for p in ("secrets.env", "services/api/secrets.env") if build_context.is_ignored(p, rules)]

    # Assert
    assert result == ignored


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


def test_pack_refuses_a_symlink_with_a_dotdot_part_even_inside_the_context(tmp_path):
    # Arrange: resolves inside the context, but the validator refuses any `..` in a link
    _tree(tmp_path, {"src/lib.py": "x"})
    (tmp_path / "pkg").mkdir()
    os.symlink("../src/lib.py", tmp_path / "pkg" / "lib.py")

    # Act / Assert
    with pytest.raises(ValueError, match="without `..`"):
        build_context.pack(tmp_path)


def test_pack_writes_plain_ustar_headers_only(tmp_path):
    # Arrange: a 200-character path (ustar prefix field) and a second name of one inode
    long_rel = "d" * 99 + "/" + "f" * 100
    _tree(tmp_path, {long_rel: "x", "a.txt": "y"})
    os.link(tmp_path / "a.txt", tmp_path / "b.txt")

    # Act
    archive = build_context.pack(tmp_path)

    # Assert: no PAX, GNU long-name or hard-link header, every member a file, directory or symlink
    raw = gzip.decompress(archive)
    assert b"PaxHeader" not in raw and b"././@LongLink" not in raw
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
    assert long_rel in {m.name for m in members}
    assert all(m.type in (tarfile.REGTYPE, tarfile.DIRTYPE, tarfile.SYMTYPE) for m in members)
    assert {m.name for m in members if m.isfile()} >= {"a.txt", "b.txt"}


def test_pack_refuses_a_path_past_the_ustar_limit(tmp_path):
    # Arrange: the last part alone is longer than 100 characters
    _tree(tmp_path, {"f" * 101: "x"})

    # Act / Assert
    with pytest.raises(ValueError, match="too long for the build context"):
        build_context.pack(tmp_path)


def test_pack_refuses_more_than_max_bytes(tmp_path):
    # Arrange
    (tmp_path / "noise.bin").write_bytes(os.urandom(64 * 1024))

    # Act / Assert
    with pytest.raises(ValueError, match="larger than 1024 bytes"):
        build_context.pack(tmp_path, max_bytes=1024)


def test_pack_refuses_a_sparse_file_whose_unpacked_size_is_past_max_bytes(tmp_path):
    # Arrange: zeros compress to almost nothing, but the node would unpack all of them
    with open(tmp_path / "zeros.bin", "wb") as f:
        f.truncate(1024 * 1024 * 1024)

    # Act / Assert
    with pytest.raises(ValueError, match="larger than 10485760 bytes"):
        build_context.pack(tmp_path, max_bytes=10 * 1024 * 1024)


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
    monkeypatch.setattr(client, "_pod_ids_before_rent", frozenset)

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


@pytest.mark.parametrize("answer", ["--yes", "declined", "expired", "slow-upload", "stalled-upload"])
def test_up_command_uploads_the_dockerfiles_directory_when_the_backend_takes_it(monkeypatch, tmp_path, answer):
    # Arrange: a declined confirmation or a spent --timeout must send nothing; an upload that
    # finishes after the budget ran out does not rent, and says the context was uploaded; the
    # upload is bounded by the budget left, and one that times out fails before the rent
    _tree(tmp_path, {"app.py": "print('hi')\n", "secrets.env": "x", ".dockerignore": "*.env\n"})
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.12-slim\nCOPY app.py /app/app.py\n")
    uploads: list[bytes] = []
    captured: dict = {}
    lium = SimpleNamespace(
        supports=lambda feature: feature == BUILD_CONTEXT,
        upload_build_context=lambda archive, timeout: _upload(archive, timeout),
        workspaces=SimpleNamespace(current=lambda: None),
    )

    def _upload(archive, timeout):
        uploads.append(archive)
        captured["upload_timeout"] = timeout
        if answer == "stalled-upload":
            raise requests.exceptions.ReadTimeout()
        return "ab" * 32

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
    prompts: list[str] = []
    monkeypatch.setattr(up_command.ui, "confirm", lambda msg, *a, **k: prompts.append(msg) or False)
    args = ["brave-fox-3a", "--dockerfile", str(dockerfile)] + (["--yes"] if answer != "declined" else [])
    if answer in ("expired", "slow-upload", "stalled-upload"):
        # deadline = 0 + 1; the clock then reads 2 before the upload, or 0 before and 2 after it
        clock = iter([0, 2] if answer == "expired" else [0, 0])
        monkeypatch.setattr(up_command.time, "monotonic", lambda: next(clock, 2))
        args += ["--timeout", "1"]
    if answer == "slow-upload":
        args.append("--json")  # the envelope carries the error code

    # Act
    result = CliRunner().invoke(up_command.up_command, args)

    # Assert
    if answer == "declined":
        assert uploads == [] and "ctx" not in captured
        assert f"Uploads {tmp_path.resolve()} as the build context" in prompts[0]
        return
    if answer == "expired":
        assert uploads == [] and "ctx" not in captured
        assert result.exit_code == 1 and "ran out before renting" in result.output
        return
    if answer != "--yes":
        assert captured["upload_timeout"] == 1  # the budget left when the upload starts
    if answer == "slow-upload":
        assert len(uploads) == 1 and "ctx" not in captured
        assert result.exit_code == 1
        envelope = json.loads(result.stderr.strip().splitlines()[-1])
        assert envelope["error"]["code"] == "timeout_before_rent"
        message = envelope["error"]["message"]
        assert "no pod was created" in message
        assert f"The build context was uploaded (sha256 {'ab' * 32}) and is kept by the API for a limited time." in message
        return
    if answer == "stalled-upload":
        assert len(uploads) == 1 and "ctx" not in captured
        assert result.exit_code == 1 and "did not finish within the --timeout budget" in result.output
        return
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
        upload_build_context=lambda archive, timeout: pytest.fail("uploaded"),
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
