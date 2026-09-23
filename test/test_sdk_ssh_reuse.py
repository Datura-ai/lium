"""`Lium` keeps one SSH connection per pod between calls (DAH-3797).

A new connection to a distant node is several round trips before a command can start;
the second command to the same pod must open only a channel on the connection it has.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

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


def _kept(lium):
    [entry] = lium._ssh_pool.values()
    return entry


def test_a_connection_with_a_call_in_flight_is_kept_past_the_idle_limit(monkeypatch, world):
    clock = [1000.0]
    monkeypatch.setattr(_ssh_reuse, "monotonic", lambda: clock[0])
    lium = _lium(monkeypatch, world)
    pod = _pod()

    with lium._remote_command(pod, "tail -f train.log"):      # a stream that outlives the idle limit
        clock[0] += _ssh_reuse.IDLE_SECONDS + 60
        assert lium.has_open_connection(pod)
        assert lium.exec(pod, command="nvidia-smi")["stdout"] == "ran nvidia-smi"
        assert world.connects == [pod.ssh_cmd] and world.clients[0].transport.active
        assert _kept(lium).users == 1

    clock[0] += _ssh_reuse.IDLE_SECONDS - 1                    # idle counts from the end of the last call
    lium.exec(pod, command="a")
    assert len(world.connects) == 1
    clock[0] += _ssh_reuse.IDLE_SECONDS + 1
    lium.exec(pod, command="b")
    assert len(world.connects) == 2 and world.closes == 1


def test_an_ssh_session_block_keeps_its_connection_past_the_idle_limit(monkeypatch, world):
    clock = [1000.0]
    monkeypatch.setattr(_ssh_reuse, "monotonic", lambda: clock[0])
    lium = _lium(monkeypatch, world)
    pod = _pod()

    with lium.ssh_session(pod):
        clock[0] += _ssh_reuse.IDLE_SECONDS * 3
        lium.exec(pod, command="a")
        assert world.connects == [pod.ssh_cmd] and world.closes == 0
    assert _kept(lium).users == 0


def test_every_call_gives_its_use_back_also_when_it_fails(monkeypatch, world, tmp_path):
    lium = _lium(monkeypatch, world)
    pod = _pod()
    lium.exec(pod, command="first")
    world.clients[0].transport.fail_after_send = True
    with pytest.raises(EOFError):
        lium.exec(pod, command="second")
    lium.download(pod, remote="/workspace/a", local=str(tmp_path / "a"))
    assert _kept(lium).users == 0


def test_a_transfer_while_another_runs_gets_an_sftp_session_of_its_own(monkeypatch, world):
    lium = _lium(monkeypatch, world)
    pod = _pod()

    with lium._sftp(pod) as first:
        with lium._sftp(pod) as second:
            assert second is not first
        assert second.closed and not first.closed
    with lium._sftp(pod) as again:
        assert again is first
    assert world.connects == [pod.ssh_cmd] and world.sftp_opens == 2


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


@pytest.mark.skipif(_no_sshd, reason="needs sshd and ssh-keygen")
def test_a_stream_longer_than_the_idle_limit_survives_a_call_made_during_it(monkeypatch, real_pod):
    lium, pod, bench = real_pod
    monkeypatch.setattr(_ssh_reuse, "IDLE_SECONDS", 1)
    output, side = "", []

    def call_from_another_thread():
        side.append(lium.exec(pod, command="echo side")["stdout"])

    for chunk in lium.stream_exec(pod, command="for i in 1 2 3 4; do echo tick$i; sleep 1; done", pty=False):
        output += chunk["data"]
        if "tick2" in output and not side:
            side.append(lium.exec(pod, command="echo inline")["stdout"])   # the same thread, inside the loop
            worker = threading.Thread(target=call_from_another_thread)
            worker.start()
            worker.join(30)

    assert [f"tick{i}" in output for i in range(1, 5)] == [True] * 4
    assert side == ["inline\n", "side\n"]
    assert bench.proxy.connections == 1


@pytest.mark.skipif(_no_sshd, reason="needs sshd and ssh-keygen")
def test_concurrent_uploads_and_downloads_to_one_pod_arrive_whole(real_pod, tmp_path):
    lium, pod, bench = real_pod
    remote_dir = bench.workdir / "remote"
    remote_dir.mkdir()
    errors = []

    def transfer(worker):
        try:
            for n in range(2):
                data = os.urandom(2 * 1024 * 1024)
                local = tmp_path / f"up-{worker}-{n}"
                local.write_bytes(data)
                remote = remote_dir / f"{worker}-{n}"
                lium.upload(pod, local=str(local), remote=str(remote))
                back = tmp_path / f"down-{worker}-{n}"
                lium.download(pod, remote=str(remote), local=str(back))
                assert hashlib.sha256(remote.read_bytes()).digest() == hashlib.sha256(data).digest()
                assert hashlib.sha256(back.read_bytes()).digest() == hashlib.sha256(data).digest()
        except Exception as e:  # noqa: BLE001 — reported by the assertion below
            errors.append(repr(e))

    workers = [threading.Thread(target=transfer, args=(w,)) for w in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(120)
    assert not any(w.is_alive() for w in workers), "a transfer hung"
    assert errors == []
    assert len(list(remote_dir.iterdir())) == 8
    assert bench.proxy.connections == 1
