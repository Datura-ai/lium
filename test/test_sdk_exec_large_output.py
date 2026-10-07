"""A command whose output exceeds the SSH channel window (paramiko: 2 MiB) only exits once the client
reads it, so exec() has to drain stdout and stderr while it waits for the exit status."""

import socket
import time
import threading

import paramiko

import pytest

from lium.sdk import Config, Lium, OutputLimitExceeded, PodInfo

OUTPUT_BYTES = 3 * 1024 * 1024  # > paramiko DEFAULT_WINDOW_SIZE (2 MiB)


class _Server(paramiko.ServerInterface):
    payload = b"x" * OUTPUT_BYTES

    def __init__(self):
        self.command = threading.Event()

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_auth_publickey(self, username, key):
        return paramiko.AUTH_SUCCESSFUL

    def get_allowed_auths(self, username):
        return "publickey"

    def check_channel_exec_request(self, channel, command):
        def run():
            time.sleep(0.2)  # after the exec request has been answered
            channel.sendall(_Server.payload)  # blocks until the client reads past the window
            channel.send_exit_status(0)
            channel.close()

        threading.Thread(target=run, daemon=True).start()
        return True


def _serve(listener, host_key):
    conn, _ = listener.accept()
    transport = paramiko.Transport(conn)
    transport.add_server_key(host_key)
    transport.start_server(server=_Server())
    channel = transport.accept(10)  # held: a dropped Channel closes itself
    transport.join(30)  # keep serving until the client hangs up
    return channel


def test_exec_returns_output_larger_than_the_channel_window(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    key = paramiko.RSAKey.generate(2048)
    key_file = tmp_path / "id_rsa"
    key.write_private_key_file(str(key_file))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=_serve, args=(listener, paramiko.RSAKey.generate(2048)), daemon=True).start()
    lium = Lium(config=Config(api_key="unused", ssh_key_path=key_file))
    pod = PodInfo(
        id="p1", name="p1", status="RUNNING", huid="p1", ssh_cmd=f"ssh root@127.0.0.1 -p {port}",
        ports={}, created_at="", updated_at="", executor=None, template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )

    result = lium.exec(pod, command="big-output", timeout=10)

    assert len(result["stdout"]) == OUTPUT_BYTES


def test_exec_stops_reading_past_max_output_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LIUM_SSH_INSECURE", "1")
    key = paramiko.RSAKey.generate(2048)
    key_file = tmp_path / "id_rsa"
    key.write_private_key_file(str(key_file))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    threading.Thread(target=_serve, args=(listener, paramiko.RSAKey.generate(2048)), daemon=True).start()
    lium = Lium(config=Config(api_key="unused", ssh_key_path=key_file))
    pod = PodInfo(
        id="p1", name="p1", status="RUNNING", huid="p1", ssh_cmd=f"ssh root@127.0.0.1 -p {port}",
        ports={}, created_at="", updated_at="", executor=None, template={},
        removal_scheduled_at=None, jupyter_installation_status=None, jupyter_url=None,
    )

    with pytest.raises(OutputLimitExceeded):
        lium.exec(pod, command="big-output", timeout=10, max_output_bytes=OUTPUT_BYTES // 2)


@pytest.mark.parametrize(
    ("wire_bytes", "tail"),
    [(3 * 1024**2, b"x"), (1024**2, b"\xf0\x9f\x98\x80"), (1024**2, b"\xff")],
)
def test_exec_limit_bounds_kept_text_not_wire_bytes(wire_bytes, tail):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import tracemalloc

    remaining = wire_bytes

    def recv(size):
        nonlocal remaining
        count = min(size, remaining)
        remaining -= count
        if not count:
            return b""
        if remaining == 0:
            return b"x" * (count - len(tail)) + tail
        return b"x" * count

    channel = SimpleNamespace(
        recv=recv, recv_stderr=lambda size: b"",
        close=lambda: None, recv_exit_status=lambda: 0,
    )
    ssh = SimpleNamespace(exec_command=lambda command: (
        SimpleNamespace(close=lambda: None),
        SimpleNamespace(channel=channel), None,
    ))
    client = SimpleNamespace(ssh_connection=lambda pod: nullcontext(ssh))
    limit = 4 * 1024**2
    tracemalloc.start()
    try:
        try:
            Lium.exec(client, SimpleNamespace(name="p", huid="p"), command="probe", max_output_bytes=limit)
        except OutputLimitExceeded:
            pass  # expected for this probe; the assertion below is the point of the test
        assert tracemalloc.get_traced_memory()[1] <= limit + 256 * 1024
    finally:
        tracemalloc.stop()
