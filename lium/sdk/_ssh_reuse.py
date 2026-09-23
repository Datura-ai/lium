"""SSH connections a :class:`~lium.sdk.Lium` client keeps open between calls to the same pod.

A new connection to a distant node is a TCP handshake, the SSH key exchange and the key
authentication: seven or so round trips before a command can start, 1-4 s per call on the
far nodes. A kept connection starts the next command after one channel open. paramiko is
imported by the caller (``lium.sdk.client``); nothing here imports it.
"""
from __future__ import annotations

import os
from contextlib import ExitStack
from time import monotonic  # bound here: the idle clock is not the one a caller's timeout loop reads
from typing import Any, Callable, ContextManager, Optional, Tuple

REUSE_ENV = "LIUM_SSH_REUSE"
# Sent while the connection sits idle, so a NAT or firewall on the way does not drop it.
KEEPALIVE_SECONDS = 15
# A connection idle longer than this is closed and opened again rather than trusted.
IDLE_SECONDS = 300
# How long opening a channel on a kept connection may take before the connection is written off.
CHANNEL_OPEN_TIMEOUT = 15


def ssh_reuse_enabled() -> bool:
    """``LIUM_SSH_REUSE=0`` gives every call a connection of its own, closed when the call ends."""
    return os.getenv(REUSE_ENV, "").strip().lower() not in ("0", "false", "no", "off")


def paramiko_transport(client: Any) -> Any:
    """The client's paramiko ``Transport``, or None for a stand-in that has none."""
    get_transport = getattr(client, "get_transport", None)
    transport = get_transport() if callable(get_transport) else None
    return transport if hasattr(transport, "open_session") and hasattr(transport, "is_active") else None


class PooledConnection:
    """One open connection (and its SFTP session, once asked for) held for later calls."""

    def __init__(self, stack: ExitStack, client: Any):
        self._stack = stack
        self.client = client
        self._sftp: Any = None
        self.last_used = monotonic()

    @classmethod
    def open(cls, connect: Callable[[], ContextManager[Any]]) -> "PooledConnection":
        """Enter ``connect()`` (``Lium.ssh_connection(pod)``) and keep it entered until :meth:`close`."""
        stack = ExitStack()
        try:
            client = stack.enter_context(connect())
        except BaseException:
            stack.close()
            raise
        transport = paramiko_transport(client)
        if transport is not None:
            transport.set_keepalive(KEEPALIVE_SECONDS)
        return cls(stack, client)

    def alive(self) -> bool:
        transport = paramiko_transport(self.client)
        return transport is None or bool(transport.is_active())

    def usable(self, now: Optional[float] = None) -> bool:
        return self.alive() and (now or monotonic()) - self.last_used < IDLE_SECONDS

    def touch(self) -> None:
        self.last_used = monotonic()

    def open_channel(self) -> Any:
        """A session channel on this connection, or None for a stand-in client without a transport.

        Raises what paramiko raises when the channel cannot be opened. Nothing has reached the
        pod then, so the caller may connect again and send its command once.
        """
        transport = paramiko_transport(self.client)
        if transport is None:
            return None
        if not transport.is_active():
            raise EOFError("the kept SSH connection is closed")
        return transport.open_session(timeout=CHANNEL_OPEN_TIMEOUT)

    def sftp(self) -> Any:
        if self._sftp is None or getattr(getattr(self._sftp, "sock", None), "closed", False):
            self._sftp = self.client.open_sftp()
        return self._sftp

    def close(self) -> None:
        sftp, self._sftp = self._sftp, None
        try:
            if sftp is not None:
                sftp.close()
        except Exception:  # noqa: BLE001 — the connection below is being closed anyway
            pass
        try:
            self._stack.close()
        except Exception:  # noqa: BLE001 — closing a connection that already died
            pass


def start_command(channel: Any, command: str, get_pty: bool = False) -> Tuple[Any, Any, Any]:
    """``exec_command`` on an already open channel: ``(stdin, stdout, stderr)`` as paramiko returns them."""
    if get_pty:
        channel.get_pty()
    channel.exec_command(command)
    return channel.makefile_stdin("wb", -1), channel.makefile("r", -1), channel.makefile_stderr("r", -1)


def close_pool(pool: dict) -> None:
    """Close every connection in ``pool`` (a client's, at ``close()`` or interpreter exit)."""
    entries = list(pool.values())
    pool.clear()
    for entry in entries:
        entry.close()
