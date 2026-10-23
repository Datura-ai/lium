"""`Lium` keeps one SSH connection per pod between calls.

A new connection to a distant node is several round trips before a command can start;
the second command to the same pod must open only a channel on the connection it has.
"""
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from lium.sdk import Config, Lium, PodInfo
from lium.sdk import _ssh_reuse
from lium.sdk import client as sdk_client


def _pod(ssh_cmd="ssh root@203.0.113.10 -p 20299", pod_id="pod-123"):
    return PodInfo(
        id=pod_id, name="warm", huid="swift-fox-c8", status="RUNNING", ssh_cmd=ssh_cmd, ports={},
        created_at="", updated_at="", executor=None, template={}, removal_scheduled_at=None,
        jupyter_installation_status=None, jupyter_url=None,
    )


class _Out:
    def __init__(self, channel, data):
        self.channel, self._data = channel, data

    def read(self, size=-1):
        # exec reads in chunks until an empty read
        data, self._data = (self._data, self._data[:0]) if size < 0 else (self._data[:size], self._data[size:])
        return data


class _In:
    def __init__(self, channel):
        self.channel = channel

    def write(self, data):
        self.channel.stdin += data

    def close(self):
        pass


class _Channel:
    def __init__(self, transport):
        self.transport = transport
        self.stdin = b""
        self.closed = False
        self.pty = False

    def get_pty(self):
        self.pty = True

    def settimeout(self, seconds):
        pass

    def invoke_subsystem(self, name):
        pass

    def exec_command(self, command):
        self.command = command
        self.transport.sent.append(command)

    def makefile_stdin(self, mode, bufsize):
        return _In(self)

    def makefile(self, mode, bufsize):
        return _Out(self, f"ran {self.command}".encode())

    def makefile_stderr(self, mode, bufsize):
        return _Out(self, b"")

    def exit_status_ready(self):
        return True

    def recv_ready(self):
        return False

    def recv_stderr_ready(self):
        return False

    def recv_exit_status(self):
        if self.transport.fail_after_send:
            raise EOFError("connection dropped mid-command")
        return 0

    def close(self):
        self.closed = True


class _Transport:
    def __init__(self, world):
        self.world = world
        self.active = True
        self.sent = world.sent
        self.fail_after_send = False
        self.fail_open = 0
        self.keepalive = None

    def is_active(self):
        return self.active

    def set_keepalive(self, seconds):
        self.keepalive = seconds

    def open_session(self, timeout=None):
        if self.fail_open:
            self.fail_open -= 1
            raise sdk_client.paramiko.SSHException("channel open refused")
        self.world.channels += 1
        return _Channel(self)


class _Sftp:
    def __init__(self, world):
        self.world = world
        self.closed = False

    def stat(self, remote):
        return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)

    def get(self, remote, local):
        self.world.gets.append(remote)

    def close(self):
        self.closed = True


class _Client:
    def __init__(self, world):
        self.world = world
        self.transport = _Transport(world)

    def get_transport(self):
        return self.transport

    def exec_command(self, command, get_pty=False):
        channel = self.transport.open_session()
        return _ssh_reuse.start_command(channel, command, get_pty=get_pty)

    def open_sftp(self):
        self.world.sftp_opens += 1
        return _Sftp(self.world)


class _World:
    """What reached the "pod": connections opened and closed, channels, commands."""

    def __init__(self):
        self.connects = []
        self.closes = 0
        self.channels = 0
        self.sent = []
        self.sftp_opens = 0
        self.gets = []
        self.clients = []


@pytest.fixture
def world(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("LIUM_SSH_REUSE", raising=False)
    return _World()


def _lium(monkeypatch, world):
    lium = Lium(Config(api_key="test", ssh_key_path="/nonexistent/key"))

    @contextmanager
    def connection(pod, timeout=30):
        held = lium._ssh_sessions.get(pod.id)   # as the real one: inside ssh_session() the held client is handed back
        if held is not None:
            yield held
            return
        world.connects.append(pod.ssh_cmd)
        client = _Client(world)
        world.clients.append(client)
        try:
            yield client
        finally:
            client.transport.active = False
            world.closes += 1

    monkeypatch.setattr(lium, "ssh_connection", connection)
    # _ssh_reuse opens SFTP sessions itself (every wait bounded): the session object is the world's
    def sftp_client(channel):
        world.sftp_opens += 1
        return _Sftp(world)

    monkeypatch.setattr(sdk_client.paramiko, "SFTPClient", sftp_client)
    return lium




def test_a_connection_that_died_while_idle_is_replaced_and_the_command_sent_once(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="first")
    world.clients[0].transport.active = False   # the node dropped the idle connection

    result = lium.exec(pod, command="second")

    assert result["stdout"] == "ran second"
    assert len(world.connects) == 2 and world.sent == ["first", "second"]


def test_a_refused_channel_open_reconnects_once_and_sends_the_command_once(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="first")
    world.clients[0].transport.fail_open = 1    # still "active", but it cannot open a channel

    lium.exec(pod, command="second")

    assert len(world.connects) == 2 and world.sent == ["first", "second"]
    assert world.clients[0].transport.active is False   # the broken connection was closed


def test_a_command_whose_connection_drops_after_it_was_sent_is_not_sent_again(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="first")
    transport = world.clients[0].transport
    transport.fail_after_send = True

    with pytest.raises(EOFError):
        lium.exec(pod, command="train.py")
    assert world.sent == ["first", "train.py"]

    transport.active = False
    lium.exec(pod, command="after")             # the dead connection is written off, a new one opened
    assert len(world.connects) == 2


























# -- against a real sshd --------------------------------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bench_warm_exec  # noqa: E402

_no_sshd = bench_warm_exec.find_sshd() is None or shutil.which("ssh-keygen") is None


@pytest.fixture
def real_pod(monkeypatch):
    try:
        bench = bench_warm_exec.Bench(rtt_ms=0, api_ms=0)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        pytest.skip(f"no local sshd: {e}")
    monkeypatch.setenv("HOME", str(bench.home))
    monkeypatch.delenv("LIUM_SSH_REUSE", raising=False)
    lium = Lium(Config(api_key="test", ssh_key_path=bench.sshd.client_key))
    pod = _pod(ssh_cmd=f"ssh {bench.sshd.user}@127.0.0.1 -p {bench.proxy.port}")
    try:
        yield lium, pod, bench
    finally:
        lium.close()
        bench.close()




























