"""`Lium.scp` accepts a directory destination (DAH-3099); `Lium.download` refuses a directory and leaves no partial file."""

import stat
from contextlib import contextmanager

from lium.sdk import Config, Lium, PodInfo


def _pod():
    return PodInfo(
        id="pod-123",
        name="scp-test",
        huid="swift-fox-c8",
        status="RUNNING",
        ssh_cmd="ssh root@pod.example.invalid -p 20299",
        ports={},
        created_at="2026-09-07T00:00:00Z",
        updated_at="2026-09-07T00:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


class _Attr:
    def __init__(self, mode):
        self.st_mode = mode


class FakeSFTP:
    def __init__(self, dirs, files=()):
        self.dirs = set(dirs)
        self.files = set(files)
        self.puts = []
        self.mkdirs = []

    def stat(self, path):
        if path in self.dirs:
            return _Attr(stat.S_IFDIR | 0o755)
        if path in self.files:
            return _Attr(stat.S_IFREG | 0o644)
        raise IOError(path)

    def mkdir(self, path):
        self.mkdirs.append(path)
        self.dirs.add(path)

    def put(self, local, remote):
        self.puts.append((local, remote))

    def close(self):
        pass


def _client_with(sftp, monkeypatch):
    class FakeSSH:
        def open_sftp(self):
            return sftp

    @contextmanager
    def fake_connection(self, pod, timeout=30, deadline=None):
        yield FakeSSH()

    monkeypatch.setattr(Lium, "ssh_connection", fake_connection)
    return Lium(Config(api_key="sk_test"))


def test_scp_file_destination_is_used_verbatim(monkeypatch):
    sftp = FakeSFTP(dirs={"/root"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="./data.csv", remote="/root/renamed.csv")
    assert sftp.puts == [("./data.csv", "/root/renamed.csv")]
    assert sftp.mkdirs == []


def test_scp_into_existing_directory_keeps_the_file_name(monkeypatch):
    sftp = FakeSFTP(dirs={"/root", "/root/datasets"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="/tmp/in/data.csv", remote="/root/datasets")
    assert sftp.puts == [("/tmp/in/data.csv", "/root/datasets/data.csv")]
    assert sftp.mkdirs == []


def test_scp_trailing_slash_creates_the_directory(monkeypatch):
    sftp = FakeSFTP(dirs={"/root"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="./data.csv", remote="/root/datasets/2026/")
    assert sftp.mkdirs == ["/root/datasets", "/root/datasets/2026"]
    assert sftp.puts == [("./data.csv", "/root/datasets/2026/data.csv")]


def test_scp_relative_directory_stays_relative_to_the_sftp_home(monkeypatch):
    sftp = FakeSFTP(dirs={"/root"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="./data.csv", remote="out/2026/")
    assert sftp.mkdirs == ["out", "out/2026"]
    assert sftp.puts == [("./data.csv", "out/2026/data.csv")]


def test_scp_tilde_prefix_means_the_sftp_home(monkeypatch):
    sftp = FakeSFTP(dirs={"/root"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="./data.csv", remote="~/data/")
    assert sftp.mkdirs == ["data"]
    assert sftp.puts == [("./data.csv", "data/data.csv")]


def test_scp_overwrites_an_existing_file(monkeypatch):
    sftp = FakeSFTP(dirs={"/root"}, files={"/root/data.csv"})
    _client_with(sftp, monkeypatch).scp(_pod(), local="./data.csv", remote="/root/data.csv")
    assert sftp.puts == [("./data.csv", "/root/data.csv")]


class FakeGetSFTP(FakeSFTP):
    """``get`` creates the local file before reading the remote one, as paramiko does."""

    def get(self, remote, local):
        open(local, "wb").close()
        if remote not in self.files:
            raise IOError(f"Failure: {remote}")


def test_download_of_a_directory_is_refused_and_writes_nothing(monkeypatch, tmp_path):
    sftp = FakeGetSFTP(dirs={"/root", "/root/out"})
    local = tmp_path / "out"
    try:
        _client_with(sftp, monkeypatch).download(_pod(), remote="/root/out", local=str(local))
    except IsADirectoryError as e:
        assert "/root/out is a directory" in str(e)
    else:
        raise AssertionError("expected IsADirectoryError")
    assert not local.exists()


def test_failed_download_removes_the_partial_file(monkeypatch, tmp_path):
    sftp = FakeGetSFTP(dirs={"/root"}, files={"/root/gone.bin"})
    sftp.files.discard("/root/gone.bin")  # stat sees nothing either: the read fails mid-way
    sftp.stat = lambda path: _Attr(stat.S_IFREG | 0o644)
    local = tmp_path / "gone.bin"
    try:
        _client_with(sftp, monkeypatch).download(_pod(), remote="/root/gone.bin", local=str(local))
    except IOError:
        pass
    else:
        raise AssertionError("expected IOError")
    assert not local.exists()


def test_cli_scp_download_of_a_directory_hints_tar_and_leaves_no_file(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from lium.cli.cli import cli

    sftp = FakeGetSFTP(dirs={"/root", "/root/some_dir"})
    _client_with(sftp, monkeypatch)
    monkeypatch.setattr(Lium, "ps", lambda self, *a, **k: [_pod()])
    monkeypatch.setenv("LIUM_API_KEY", "sk_test")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli, ["scp", "swift-fox-c8", "/root/some_dir", "./local/", "-d"])

    output = " ".join(result.output.split())  # Rich wraps long lines
    assert result.exit_code != 0
    assert "lium exec swift-fox-c8 'tar czf /root/some_dir.tar.gz -C /root some_dir/.'" in output
    assert "lium scp swift-fox-c8 /root/some_dir.tar.gz -d" in output
    assert list((tmp_path / "local").iterdir()) == []


def test_failed_download_keeps_an_existing_local_file(monkeypatch, tmp_path):
    sftp = FakeSFTP(dirs={"/root"}, files={"/root/data.csv"})

    def refuse(remote, local):
        raise PermissionError(local)  # paramiko on a read-only destination, before writing anything

    sftp.get = refuse
    local = tmp_path / "data.csv"
    local.write_text("keep me")
    try:
        _client_with(sftp, monkeypatch).download(_pod(), remote="/root/data.csv", local=str(local))
    except PermissionError:
        pass
    else:
        raise AssertionError("expected PermissionError")
    assert local.read_text() == "keep me"


def _directory_hint(monkeypatch, tmp_path, remote):
    from click.testing import CliRunner

    from lium.cli.cli import cli

    sftp = FakeGetSFTP(dirs={"/root", "/root/out", "out", ".", "/"})
    _client_with(sftp, monkeypatch)
    monkeypatch.setattr(Lium, "ps", lambda self, *a, **k: [_pod()])
    monkeypatch.setenv("LIUM_API_KEY", "sk_test")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli, ["scp", "swift-fox-c8", remote, "./local/", "-d"])
    assert result.exit_code != 0
    return " ".join(result.output.split())


def test_cli_scp_directory_hint_for_a_relative_path_archives_from_home(monkeypatch, tmp_path):
    output = _directory_hint(monkeypatch, tmp_path, "out")
    assert "lium exec swift-fox-c8 'tar czf out.tar.gz out/.'" in output


def test_cli_scp_directory_hint_for_dot_or_root_gives_no_command(monkeypatch, tmp_path):
    for remote in (".", "/"):
        output = _directory_hint(monkeypatch, tmp_path, remote)
        assert "tar czf" not in output
        assert "Archive it on the pod with tar" in output


def test_failed_download_keeps_an_existing_dangling_symlink(monkeypatch, tmp_path):
    sftp = FakeSFTP(dirs={"/root"}, files={"/root/data.csv"})

    def refuse(remote, local):
        raise FileNotFoundError(local)  # the link target's parent is missing

    sftp.get = refuse
    local = tmp_path / "latest.bin"
    local.symlink_to(tmp_path / "missing" / "model.bin")
    try:
        _client_with(sftp, monkeypatch).download(_pod(), remote="/root/data.csv", local=str(local))
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError")
    assert local.is_symlink()
