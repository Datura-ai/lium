"""The paramiko side of host-key pinning (DAH-2904): the two ``MissingHostKeyPolicy`` subclasses
``ssh_connection`` installs and the fingerprint their messages print.

They subclass paramiko at class-creation time, so they live here rather than in ``client.py``, which
imports paramiko lazily (DAH-3053) and imports this module inside ``ssh_connection``. The path-side
helpers (``known_hosts_path``, ``forget_host_key``, ``ssh_insecure``) stay in ``client.py``: ``rsync``,
``reboot``, ``down`` and ``edit`` use them without paramiko.
"""

import base64
import hashlib
import warnings

import paramiko

from .client import _SSH_INSECURE_ENV


def host_key_fingerprint(key: paramiko.PKey) -> str:
    """``SHA256:<base64>`` as ``ssh-keygen -lf`` prints it, so a user can compare the two."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class _PinOnFirstUsePolicy(paramiko.MissingHostKeyPolicy):
    """Trust-on-first-use: record the key of a pod we have never talked to.

    Later connections to the same pod are checked against the recorded key by
    paramiko itself (``BadHostKeyException`` on mismatch); ``ssh_connection``
    turns that into :class:`LiumHostKeyError`.
    """

    def missing_host_key(self, client, hostname, key):  # noqa: D401 - paramiko interface
        client._host_keys.add(hostname, key.get_name(), key)
        if client._host_keys_filename is not None:
            client.save_host_keys(client._host_keys_filename)
        fp = host_key_fingerprint(key)
        warnings.warn(
            f"Pinning {key.get_name()} host key {fp} for {hostname} "
            f"(first connection to this pod; {_SSH_INSECURE_ENV}=1 disables pinning)",
            stacklevel=2,
        )


class _InsecureAcceptPolicy(paramiko.MissingHostKeyPolicy):
    """Accept whatever key the host presents. Installed only under ``LIUM_SSH_INSECURE=1``.

    This is the pre-pinning behaviour (paramiko's ``AutoAddPolicy``) spelled out:
    the key is kept for the life of this client so the connection proceeds, nothing
    is written to disk, and every acceptance is reported so the opt-out is never
    silent. The default path uses :class:`_PinOnFirstUsePolicy`.
    """

    def missing_host_key(self, client, hostname, key):  # noqa: D401 - paramiko interface
        client.get_host_keys().add(hostname, key.get_name(), key)
        warnings.warn(
            f"Accepting unverified {key.get_name()} host key {host_key_fingerprint(key)} for "
            f"{hostname}: {_SSH_INSECURE_ENV}=1 disabled host key verification",
            stacklevel=2,
        )
