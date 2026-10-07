"""SSH connections a :class:`~lium.sdk.Lium` client keeps open between calls to the same pod.

A new connection to a distant node is a TCP handshake, the SSH key exchange and the key
authentication: seven or so round trips before a command can start, 1-4 s per call on the
far nodes. A kept connection starts the next command after one channel open. paramiko is
imported by the caller (``lium.sdk.client``); here it is imported only to open an SFTP session.
"""
from __future__ import annotations

import os
import threading
from contextlib import ExitStack
from time import monotonic  # bound here: the idle clock is not the one a caller's timeout loop reads
from typing import Any, Callable, ContextManager, Optional, Tuple

REUSE_ENV = "LIUM_SSH_REUSE"
# Sent while the connection sits idle, so a NAT or firewall on the way does not drop it.
KEEPALIVE_SECONDS = 15
# A connection that no call has used for this long (counted from the end of the last call) is
# closed; the next call opens a new one. A connection with a call in flight is kept.
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
    """One open connection (and its SFTP session, once asked for) held for later calls.

    ``users`` counts the calls running on it (a command, a stream, a transfer, an
    :meth:`Lium.ssh_session` block); while it is above zero the connection is in use and
    never idle. The idle clock starts when the last of them ends.
    """

    def __init__(self, stack: ExitStack, client: Any):
        self._stack = stack
        self.client = client
        self._sftp: Any = None
        self._sftp_lock = threading.Lock()   # paramiko's SFTPClient serves one caller at a time
        self._users_lock = threading.Lock()
        self.users = 0
        self.last_used = monotonic()
        self._timer: Optional[threading.Timer] = None
        self._pool: Optional[dict] = None          # the pool (not the client) this entry is listed in
        self._pool_lock: Optional[threading.Lock] = None

    def listed_in(self, pool: dict, lock: threading.Lock) -> None:
        """Tell the entry which pool holds it, so an idle expiry can take it out of there."""
        self._pool, self._pool_lock = pool, lock

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
        if not self.alive():
            return False
        with self._users_lock:
            return self.users > 0 or (now or monotonic()) - self.last_used < IDLE_SECONDS

    def acquire(self) -> None:
        with self._users_lock:
            self.users += 1
            self._cancel_timer()

    def release(self) -> None:
        with self._users_lock:
            self.users = max(0, self.users - 1)
            self.last_used = monotonic()
            if self.users == 0:
                self._cancel_timer()
                # Nothing else wakes an idle connection: without a request for the same pod it would
                # stay open (and its pod's SSH session with it) until the process ends.
                self._timer = threading.Timer(IDLE_SECONDS, self._expire)
                self._timer.daemon = True
                self._timer.start()

    def _cancel_timer(self) -> None:  # call with _users_lock held
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _expire(self) -> None:
        """Close the connection when it has had no user for ``IDLE_SECONDS``; checked under the locks
        a taker holds, so a connection being taken at this moment is never closed."""
        pool, pool_lock = self._pool, self._pool_lock
        guard = pool_lock if pool_lock is not None else threading.Lock()
        with guard:
            with self._users_lock:
                if self.users > 0 or monotonic() - self.last_used < IDLE_SECONDS:
                    return
                self._timer = None
            if pool is not None:
                for key in [k for k, kept in pool.items() if kept is self]:
                    pool.pop(key, None)
        self.close()

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

    def take_sftp(self) -> Tuple[Any, bool]:
        """``(sftp, shared)``: the kept SFTP session when no other call is using it, else one of its own.

        Hand it back with :meth:`give_back_sftp`. Two transfers at once each get a session, so
        neither waits for the other and no request of one lands in the other's.
        """
        if not self._sftp_lock.acquire(blocking=False):
            return self._open_sftp(), False
        try:
            if self._sftp is not None and getattr(getattr(self._sftp, "sock", None), "closed", False):
                self._sftp = None
            if self._sftp is None:
                self._sftp = self._open_sftp()
            else:
                self._ping(self._sftp)
            return self._sftp, True
        except BaseException:
            sftp, self._sftp = self._sftp, None
            self._sftp_lock.release()
            try:
                if sftp is not None:
                    sftp.close()
            except Exception:  # noqa: BLE001 — a session on a connection that is being written off
                pass
            raise

    def _open_sftp(self) -> Any:
        """A new SFTP session, every wait bounded by ``CHANNEL_OPEN_TIMEOUT`` (paramiko's own default is an hour)."""
        transport = paramiko_transport(self.client)
        if transport is None:
            return self.client.open_sftp()
        import paramiko

        channel = transport.open_session(timeout=CHANNEL_OPEN_TIMEOUT)
        # invoke_subsystem waits for the peer's acknowledgement with no timeout; closing the channel releases it
        timed_out = threading.Event()

        def _give_up() -> None:
            timed_out.set()
            channel.close()

        deadline = threading.Timer(CHANNEL_OPEN_TIMEOUT, _give_up)
        deadline.daemon = True
        deadline.start()
        try:
            channel.settimeout(CHANNEL_OPEN_TIMEOUT)   # the version exchange below reads under it
            channel.invoke_subsystem("sftp")
            sftp = paramiko.SFTPClient(channel)
        except BaseException as exc:
            channel.close()
            if timed_out.is_set():
                # Only this channel failed: a ChannelException keeps the kept connection (and the commands
                # running on it); a bare SSHException from the closed channel would write the connection off.
                raise paramiko.ChannelException(
                    paramiko.common.OPEN_FAILED_CONNECT_FAILED, "the SFTP subsystem did not answer in time"
                ) from exc
            raise
        finally:
            deadline.cancel()
        channel.settimeout(None)
        return sftp

    @staticmethod
    def _ping(sftp: Any) -> None:
        """One round trip on a kept session, bounded: a peer that went away silently (a laptop that slept,
        a network switch) never answers, and a transfer would then block until TCP gave up (~15 min).
        ``socket.timeout`` is an ``OSError``: the caller treats it as a dead connection and connects again."""
        get_channel = getattr(sftp, "get_channel", None)
        channel = get_channel() if callable(get_channel) else None
        if channel is None or not hasattr(sftp, "normalize"):
            return
        channel.settimeout(CHANNEL_OPEN_TIMEOUT)
        try:
            sftp.normalize(".")
        finally:
            channel.settimeout(None)

    def give_back_sftp(self, sftp: Any, shared: bool) -> None:
        if shared:
            self._sftp_lock.release()
            return
        try:
            sftp.close()
        except Exception:  # noqa: BLE001 — a per-call session on a connection that may have died
            pass

    def close(self) -> None:
        with self._users_lock:
            self._cancel_timer()
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
