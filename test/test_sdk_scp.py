"""`Lium.scp` accepts a directory destination (DAH-3099)."""

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
    def fake_connection(self, pod, timeout=30):
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
