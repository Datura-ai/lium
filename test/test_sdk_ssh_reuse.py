"""`Lium` keeps one SSH connection per pod between calls (DAH-3797).

A new connection to a distant node is several round trips before a command can start;
the second command to the same pod must open only a channel on the connection it has.
"""
from contextlib import contextmanager

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

    def read(self):
        return self._data


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
        world.connects.append(pod.ssh_cmd)
        client = _Client(world)
        world.clients.append(client)
        try:
            yield client
        finally:
            client.transport.active = False
            world.closes += 1

    monkeypatch.setattr(lium, "ssh_connection", connection)
    return lium


def test_two_commands_to_one_pod_open_one_connection(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()

    first = lium.exec(pod, command="echo 1")
    second = lium.exec(pod, command="echo 2", env={"TOKEN": "abc"})

    assert world.connects == [pod.ssh_cmd] and world.closes == 0
    assert world.channels == 2 and world.sent[0] == "echo 1" and world.sent[1].endswith("&& echo 2")
    assert first["stdout"] == "ran echo 1" and first["exit_code"] == 0 and second["success"]
    assert world.clients[0].transport.keepalive == _ssh_reuse.KEEPALIVE_SECONDS
    assert lium.has_open_connection(pod)


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


def test_a_new_ssh_address_for_the_pod_gets_a_new_connection(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    lium.exec(_pod(), command="a")
    lium.exec(_pod(ssh_cmd="ssh root@203.0.113.11 -p 20300"), command="b")

    assert world.connects == ["ssh root@203.0.113.10 -p 20299", "ssh root@203.0.113.11 -p 20300"]


def test_a_connection_idle_past_the_limit_is_opened_again(monkeypatch, world):
    clock = [1000.0]
    monkeypatch.setattr(_ssh_reuse, "monotonic", lambda: clock[0])
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="a")
    clock[0] += _ssh_reuse.IDLE_SECONDS + 1

    assert not lium.has_open_connection(pod)
    lium.exec(pod, command="b")
    assert len(world.connects) == 2 and world.closes == 1


@pytest.mark.parametrize("operation", ["down", "reboot"])
def test_removing_or_rebooting_the_pod_closes_its_connection(monkeypatch, world, operation):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="a")
    monkeypatch.setattr(lium, "_request", lambda *a, **k: type("R", (), {"json": lambda self: {}})())

    getattr(lium, operation)(pod)

    assert world.closes == 1 and not lium.has_open_connection(pod)
    lium.exec(pod, command="b")
    assert len(world.connects) == 2


def test_close_and_the_with_block_close_every_kept_connection(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    lium.exec(_pod(), command="a")
    lium.exec(_pod(pod_id="pod-456", ssh_cmd="ssh root@203.0.113.12 -p 22"), command="b")
    lium.close()
    assert world.closes == 2

    with _lium(monkeypatch, world) as scoped:
        scoped.exec(_pod(), command="c")
    assert world.closes == 3


def test_ssh_reuse_off_connects_and_closes_per_call(monkeypatch, world):
    monkeypatch.setenv("LIUM_SSH_REUSE", "0")
    lium = _lium(monkeypatch, world)
    pod = _pod()

    lium.exec(pod, command="a")
    lium.exec(pod, command="b")

    assert len(world.connects) == 2 and world.closes == 2
    assert not lium.has_open_connection(pod)


def test_downloads_share_one_sftp_session_on_the_kept_connection(monkeypatch, world, tmp_path):
    lium = _lium(monkeypatch, world)
    pod = _pod()

    lium.download(pod, remote="/workspace/a", local=str(tmp_path / "a"))
    lium.download(pod, remote="/workspace/b", local=str(tmp_path / "b"))
    lium.exec(pod, command="ls")

    assert world.connects == [pod.ssh_cmd] and world.sftp_opens == 1
    assert world.gets == ["/workspace/a", "/workspace/b"]


def test_stream_exec_runs_on_the_kept_connection_with_the_pty_it_asked_for(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="a")

    channels = []
    original = _Transport.open_session

    def recording(self, timeout=None):
        channel = original(self, timeout)
        channels.append(channel)
        return channel

    monkeypatch.setattr(_Transport, "open_session", recording)
    assert list(lium.stream_exec(pod, command="tail log", pty=True)) == []

    assert world.connects == [pod.ssh_cmd]
    assert channels[0].pty and channels[0].command == "tail log" and channels[0].closed
