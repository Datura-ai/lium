"""Lium SDK - Clean, Unix-style SDK for GPU pod management."""

import base64
import getpass
import hashlib
import ipaddress
import os
import re
import shlex
import socket
import subprocess
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Callable, Dict, Generator, List, Optional, Sequence, Union
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import paramiko
import requests
from dotenv import load_dotenv

from lium.__about__ import __version__ as fallback_version

from .config import Config
from .exceptions import (
    LiumAuthError,
    LiumError,
    LiumHostKeyError,
    LiumNotFoundError,
    LiumInsufficientBalanceError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
    PodStartError,
)
from .models import (
    BackupConfig,
    BackupLog,
    ExecutorInfo,
    PodInfo,
    RentResult,
    RestoreLog,
    SSHKey,
    Template,
    VolumeInfo,
)
from .ssh_key_cache import fingerprint, load_cache, save_cache
from .utils import extract_gpu_type, generate_huid, gpu_short_matches, with_retry
from .workspaces import WorkspacesClient

# The backend feature `Lium.rent` looks for on GET /version before using POST /executors/rent-by-spec.
RENT_BY_SPEC = "rent_by_spec"
# Node specs report RAM and disk in KiB and GPU memory in MiB.
_KIB_PER_GB = 1024 * 1024
_MIB_PER_GB = 1024

load_dotenv()

# A POSIX shell identifier: what ``export`` accepts on the pod.
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")  # checked with fullmatch: `$` would let a trailing newline through
# HTTP methods that are safe to repeat after a lost response; see ``Lium._request``.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Public API key for the pay API (pay-tao-api-v2). Single source of truth so the
# literal is not re-typed across every pay-API call site.
_PAY_API_KEY = "6RhXQ788J9BdnqeLua8z7ZSkXBDahclxhwjMB17qW1M"

# Set LIUM_SSH_INSECURE=1 to restore the old behaviour (accept any host key, never pin).
_SSH_INSECURE_ENV = "LIUM_SSH_INSECURE"
_POD_ID_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def ssh_insecure() -> bool:
    """True when host-key pinning is disabled via ``LIUM_SSH_INSECURE=1``."""
    return os.getenv(_SSH_INSECURE_ENV, "").strip().lower() in ("1", "true", "yes")


def known_hosts_path(pod: Union[PodInfo, str]) -> Path:
    """Per-pod known_hosts file: ``~/.lium/known_hosts/<pod id>``.

    Pods are ephemeral and executors reuse ``host:port`` for new rentals, so a
    single OpenSSH-style file keyed by address would flag every new pod on a
    recycled address as a key change. Keying by pod id pins the key for the
    lifetime of the pod and lets a fresh pod on the same address start clean.
    """
    pod_id = pod if isinstance(pod, str) else pod.id
    safe_id = _POD_ID_SAFE.sub("_", pod_id or "unknown")
    return Path.home() / ".lium" / "known_hosts" / safe_id


def host_key_fingerprint(key: paramiko.PKey) -> str:
    """``SHA256:<base64>`` as ``ssh-keygen -lf`` prints it, so a user can compare the two."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def forget_host_key(pod: Union[PodInfo, str]) -> None:
    """Drop the pinned host key of a pod (its container, and so its key, is being replaced)."""
    try:
        known_hosts_path(pod).unlink()
    except FileNotFoundError:
        pass  # nothing pinned yet: forgetting is idempotent
    except OSError:
        pass  # best effort; a pin we cannot delete surfaces as LiumHostKeyError on the next connection, which names the file


def _ensure_known_hosts_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best effort (read-only or foreign filesystem); the per-file 0600 mode below is what protects the pins


def _ensure_known_hosts_file(path: Path) -> None:
    _ensure_known_hosts_dir(path)
    if not path.exists():
        path.touch(mode=0o600)


def openssh_host_key_options(pod: PodInfo, *, create_pin_file: bool = True) -> List[str]:
    """The ``-o`` arguments that make the OpenSSH client check a pod's host key.

    Pinned per pod under ``~/.lium/known_hosts/<pod id>`` (created here), accepted
    on the first connection and refused by ssh itself when the pod later presents
    a different key — the same rule :meth:`Lium.ssh_connection` applies through
    paramiko. ``LIUM_SSH_INSECURE=1`` returns the old accept-anything options.
    ``create_pin_file=False`` creates only the directory (a listing that prints the
    command should not leave a file per pod behind; ssh writes the file itself).
    """
    if ssh_insecure():
        return ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
    hosts_file = known_hosts_path(pod)
    if create_pin_file:
        _ensure_known_hosts_file(hosts_file)
    else:
        try:
            _ensure_known_hosts_dir(hosts_file)
        except OSError:
            pass  # a read-only home: the command is still right, ssh says it could not record the key
    # OpenSSH splits an unquoted UserKnownHostsFile value on whitespace (it takes several files); the
    # quotes keep a home directory with a space in it as one path.
    return ["-o", "StrictHostKeyChecking=accept-new", "-o", f'UserKnownHostsFile="{hosts_file}"']


_SSH_USER_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")   # no leading "-": ssh would read the destination as an option
_SSH_HOSTNAME_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")


def ssh_target(ssh_cmd: Optional[str]) -> tuple[str, str, int]:
    """``(user, host, port)`` from the API's ``ssh_connect_cmd``, or ``ValueError``.

    The API sends ``ssh <user>@<host> -p <port>`` and nothing else. Only that shape
    (``-p <port>`` optional, default 22) is accepted — every token is checked, so a
    value that carries anything besides a user, an address and a port (an extra ssh
    option, a shell character) is refused instead of being handed to ssh or to a shell.
    """
    if not ssh_cmd:
        raise ValueError("No SSH command for this pod")
    try:
        tokens = shlex.split(ssh_cmd)
    except ValueError as e:
        raise ValueError(f"Unexpected ssh command from the API: {ssh_cmd!r} ({e})") from e
    if len(tokens) not in (2, 4) or tokens[0] != "ssh":
        raise ValueError(f"Unexpected ssh command from the API: {ssh_cmd!r}")
    user, sep, host = tokens[1].partition("@")
    if not sep or len(user) > 32 or len(host) > 253 or not _SSH_USER_RE.fullmatch(user):
        raise ValueError(f"Unexpected ssh command from the API: {ssh_cmd!r}")
    if not _SSH_HOSTNAME_RE.fullmatch(host):
        try:
            if "%" in host:  # an IPv6 scope id is not an address the API sends
                raise ValueError(host)
            ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"Unexpected ssh command from the API: {ssh_cmd!r}") from None
    port = 22
    if len(tokens) == 4:
        if tokens[2] != "-p" or not re.fullmatch(r"[0-9]{1,5}", tokens[3]) or not 1 <= int(tokens[3]) <= 65535:
            raise ValueError(f"Unexpected ssh command from the API: {ssh_cmd!r}")
        port = int(tokens[3])
    return user, host, port


def pod_ssh_command(pod: PodInfo) -> Optional[str]:
    """The pod's ssh command for a shell, with the host-key options and without a key.

    ``ssh -p <port> -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=<pin> <user>@<host>``
    (:func:`ssh_target` + :func:`openssh_host_key_options`, shell-quoted): what
    ``lium ps --format json`` and ``lium describe`` show as ``ssh_command``. It
    carries no ``-i <key>`` — the key path lives in the SDK config, not in the pod
    record; :meth:`Lium.ssh` adds it. Unlike :meth:`Lium.ssh` it creates no pin
    file (only the directory): a listing leaves nothing per pod behind. None when
    the pod has no ssh command yet or the API's value is not ``ssh <user>@<host>
    [-p <port>]``; both views keep the raw ``ssh_cmd`` next to it.
    """
    try:
        user, host, port = ssh_target(pod.ssh_cmd)
    except ValueError:
        return None
    options = openssh_host_key_options(pod, create_pin_file=False)
    return shlex.join(["ssh", "-p", str(port), *options, f"{user}@{host}"])


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


def _satisfies_spec(executor: ExecutorInfo, spec: Dict[str, Any]) -> bool:
    """The client-side reading of a rent spec, for backends without ``rent_by_spec``.

    Mirrors the server's constraints on the fields ``GET /executors`` carries; a node that
    does not report a figure fails the floor on it, as on the server.
    """
    specs = executor.specs or {}
    gpu_details = (specs.get("gpu") or {}).get("details") or []
    gpu_detail = gpu_details[0] if gpu_details and isinstance(gpu_details[0], dict) else {}
    disk = specs.get("hard_disk") or {}
    disk_kib = disk.get("free") if disk.get("free") is not None else disk.get("total")
    floors = {
        "min_vram_gb": (gpu_detail.get("capacity") or 0) / _MIB_PER_GB if gpu_detail.get("capacity") else None,
        "min_cpus": (specs.get("cpu") or {}).get("count"),
        "min_ram_gb": ((specs.get("ram") or {}).get("total") or 0) / _KIB_PER_GB if (specs.get("ram") or {}).get("total") else None,
        "min_disk_gb": disk_kib / _KIB_PER_GB if disk_kib else None,
        "min_download_mbps": executor.effective_download_speed_mbps,
        "min_ports": executor.available_port_count,
    }
    if executor.gpu_count != spec.get("gpu_count", 1):
        return False
    for key, have in floors.items():
        wanted = spec.get(key)
        if wanted is not None and (have is None or have < wanted):
            return False
    if spec.get("max_price_per_gpu_hour") is not None and (
        executor.price_per_gpu is None or executor.price_per_gpu > spec["max_price_per_gpu_hour"]
    ):
        return False
    if spec.get("country") and ((executor.location or {}).get("country_code") or "").upper() != spec["country"].upper():
        return False
    if spec.get("docker_in_docker") and not executor.docker_in_docker:
        return False
    if spec.get("interconnect") == "nvlink" and (specs.get("interconnect") or {}).get("nvlink") is not True:
        return False
    return True


_USD = r"\$([0-9][0-9,]*(?:\.[0-9]+)?)"
# The platform's balance refusals: "Insufficient balance" from the auth dependency and
# "Insufficient balance. This node costs $X/hour, so renting it requires at least $Y
# (N minutes of runtime). Your balance is $Z." from the rent path.
_REQUIRED_RE = re.compile(r"requires at least " + _USD, re.I)
_AVAILABLE_RE = re.compile(r"balance is " + _USD, re.I)


def _response_error_code(response: requests.Response) -> Optional[str]:
    """The stable ``error.code`` of the platform's error body, when it sends one.

    lium-platform#210 (DAH-3056) answers every 4xx/5xx with
    ``{"error": {"code", "message", "hint", "request_id"}, ...}``; older servers
    send ``error`` as a string or not at all, and then this is ``None``.
    """
    try:
        payload = response.json()
    except ValueError:  # not a JSON body: older servers answer plain text, and then there is no code
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else None


def permission_error(
    detail: str, code: Optional[str] = None, key: Optional[str] = None, **context: Optional[str]
) -> LiumPermissionError:
    """The exception for a 403: :class:`LiumInsufficientBalanceError` when the server
    refused for lack of funds (with ``required``/``available`` when it said them),
    else a plain :class:`LiumPermissionError`.

    ``code`` is the platform's structured ``error.code`` when the response carried
    one (:func:`_response_error_code`); it decides. Without it the message text
    decides, which is what every server before lium-platform#210 sends. ``key``
    (the API key's fingerprint and source) is appended so the message says which
    key the server refused. ``code`` and ``context`` (:func:`_error_context`'s
    hint/request_id) are carried on the exception.
    """
    message = f"Permission denied: {detail}" + (f" ({key})" if key else "")
    if code is not None:
        insufficient = code == "insufficient_balance"
    else:
        insufficient = "insufficient balance" in (detail or "").lower()
    if not insufficient:
        return LiumPermissionError(message, code=code, **context)

    def usd(match: Optional[re.Match]) -> Optional[float]:
        return float(match.group(1).replace(",", "")) if match else None

    return LiumInsufficientBalanceError(
        message,
        required=usd(_REQUIRED_RE.search(detail)),
        available=usd(_AVAILABLE_RE.search(detail)),
        code=code,
        **context,
    )


def _response_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return response.text or "Request failed"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    response_message = payload.get("message") if isinstance(payload, dict) else None
    validation_errors = (
        payload.get("validation_errors") if isinstance(payload, dict) else None
    )
    structured_error = (
        detail
        if isinstance(detail, dict)
        else response_message if isinstance(response_message, dict) else None
    )
    if isinstance(validation_errors, list) and validation_errors:
        messages: list[str] = []
        for error in validation_errors:
            if not isinstance(error, dict):
                messages.append(str(error))
                continue
            field = error.get("field")
            reason = error.get("message") or error.get("msg") or "Invalid value"
            messages.append(f"{field}: {reason}" if field else str(reason))
        validation_summary = "; ".join(messages)
        message = (
            f"{response_message}: {validation_summary}"
            if isinstance(response_message, str)
            else validation_summary
        )
    elif isinstance(detail, list) and detail:
        message = detail[0].get("msg") if isinstance(detail[0], dict) else str(detail[0])
    elif structured_error:
        message = structured_error.get("message") or "Request failed"
        if structured_error.get("active_operation_id"):
            message += f" (active operation: {structured_error['active_operation_id']})"
    else:
        message = detail or response_message
    return str(message or "Request failed")


def _int_or_none(row: Dict[str, Any], key: str) -> Optional[int]:
    """``int(row[key])``, or None when the key is absent, null or not a number."""
    try:
        return int(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def _pod_gpu_count(row: Dict[str, Any]) -> Optional[int]:
    """The pod's own billed GPU count from a ``/pods`` row (a string in the payload), or None."""
    return _int_or_none(row, "gpu_count")


def _error_context(response: requests.Response) -> dict:
    """code/hint/request_id from the API's error envelope (``error: {...}``) and the
    ``X-Request-Id`` header, for the exception's attributes. Empty when absent (older servers)."""
    try:
        error = response.json().get("error")
    except Exception:
        error = None
    error = error if isinstance(error, dict) else {}
    headers = getattr(response, "headers", None) or {}

    def text(value: Any) -> Optional[str]:
        # only non-empty strings: the fields are printed and compared as text, and a server
        # (or a proxy) sending a number or an object here must not break the error path
        return value if isinstance(value, str) and value else None

    return {
        "code": _response_error_code(response),  # the same field; #219 reads it through this helper too
        "hint": text(error.get("hint")),
        "request_id": text(error.get("request_id")) or text(headers.get("X-Request-Id")),
    }


def _get_client_version() -> str:
    try:
        return version("lium.io")
    except PackageNotFoundError:
        return os.environ.get("LIUM_BUILD_VERSION", fallback_version)


@dataclass(frozen=True)
class AlphaQuote:
    """USD -> alpha quote from ``GET /balance/convert/alpha``.

    ``netuid`` is the subnet the alpha must be transferred on (the same subnet the
    pay-tao-api-v2 listener credits), so it — not a hardcoded constant — drives the
    on-chain ``transfer_stake``.
    """

    usd: Decimal           # echoes the API's ``original``
    alpha_amount: Decimal  # the API's ``converted`` (raw Decimal; floored by the caller)
    rate: Decimal          # the API's ``rate`` = USD per alpha
    netuid: int            # the API's ``netuid`` — drives the transfer


# Main SDK Class
class Lium:
    """Clean Unix-style SDK for Lium."""

    def __init__(self, config: Optional[Config] = None, source: str = "sdk", workspace: Optional[str] = None):
        """``workspace`` picks the API key saved for that workspace (``[workspace.<name>]`` in
        ~/.lium/config.ini, written by ``lium keys create --workspace … --save``); a key acts in exactly
        one workspace, so choosing the workspace means choosing the key (lium-platform DAH-2986), and
        ``ValueError`` is raised when none is saved for it rather than running as another key."""
        self.config = config or Config.load(workspace=workspace)
        self.source = source
        self.headers = {
            "X-API-KEY": self.config.api_key,
            "X-Source": source,
            "X-Lium-Client-Version": _get_client_version(),
        }
        self._features: Optional[set] = None
        self.workspaces = WorkspacesClient(self)
        self._ssh_sessions: Dict[str, paramiko.SSHClient] = {}  # pod id -> connection held by ssh_session()

    def features(self) -> set:
        """Optional API capabilities the backend advertises on ``GET /version``.

        Read once per client. A backend that predates the list, or one that cannot be
        reached, advertises nothing — callers then take their client-side path.
        """
        if self._features is None:
            try:
                data = self._request("GET", "/version").json()
                names = data.get("features") if isinstance(data, dict) else None
                self._features = {str(name) for name in names} if isinstance(names, list) else set()
            except Exception:  # noqa: BLE001 — an unreachable /version is "no features", not an error
                self._features = set()
        return self._features

    def supports(self, feature: str) -> bool:
        """Whether the backend advertises ``feature`` (see :meth:`features`)."""
        return feature in self.features()

    def _request(
        self,
        method: str,
        endpoint: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        retry: Optional[bool] = None,
        **kwargs,
    ) -> requests.Response:
        """Make API request with error handling.

        Transient failures (429, 5xx, network errors) are retried up to three
        times for idempotent methods (``GET``, ``HEAD``, ``OPTIONS``). Anything
        that mutates (``POST``, ``PUT``, ``PATCH``, ``DELETE``) is repeated only
        after a 429: the server answered instead of running the request, so
        sending it again cannot duplicate anything. A 5xx or a lost connection
        is not repeated for them — a timed-out POST may well have succeeded
        server-side, and repeating it blindly creates a second template, volume,
        backup or pod; a repeated DELETE turns a completed removal into "not
        found". ``retry=True`` retries every transient failure, ``retry=False``
        sends exactly once, whatever the method.
        """
        if retry is True or (retry is None and method.upper() in IDEMPOTENT_METHODS):
            return self._request_with_retry(method, endpoint, base_url=base_url, headers=headers, **kwargs)
        if retry is False:
            return self._request_once(method, endpoint, base_url=base_url, headers=headers, **kwargs)
        return self._request_backing_off_rate_limits(method, endpoint, base_url=base_url, headers=headers, **kwargs)

    @with_retry()
    def _request_with_retry(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        return self._request_once(method, endpoint, **kwargs)

    @with_retry(exceptions=(LiumRateLimitError,))
    def _request_backing_off_rate_limits(self, method: str, endpoint: str, **kwargs) -> requests.Response:
        return self._request_once(method, endpoint, **kwargs)

    def _request_once(
        self,
        method: str,
        endpoint: str,
        base_url: Optional[str] = None,
        headers: Optional[Dict[str, str]] = None,
        **kwargs,
    ) -> requests.Response:
        url = f"{base_url or self.config.base_url}/{endpoint.lstrip('/')}"
        request_headers = headers or self.headers
        timeout = kwargs.pop("timeout", 30)
        resp = requests.request(method, url, headers=request_headers, timeout=timeout, **kwargs)
        try:
            self._raise_for_status(resp, key=self.config.api_key_description)
        except Exception:
            # A streamed response (logs) that is never read keeps its socket
            # until garbage collection; the caller only gets the exception.
            close = getattr(resp, "close", None)
            if callable(close):
                close()
            raise
        return resp

    @staticmethod
    def _raise_for_status(resp: requests.Response, key: str = "") -> None:
        """Map a non-2xx response to the SDK exception for its status.

        The single place this mapping lives; every HTTP path (``_request`` and
        the streaming ``logs``) goes through it so a 403 reads the same
        everywhere. ``key`` is the caller's ``api_key_description``; it is named
        in the 401/403 message so the user knows which key to fix.
        """
        if resp.ok:
            return
        context = _error_context(resp)
        # Auth failures name the key that was sent: two commands can resolve
        # different keys (environment versus config file), and "invalid API key"
        # alone does not say which one to fix.
        if resp.status_code == 401:
            raise LiumAuthError(f"Invalid API key ({key})" if key else "Invalid API key", **context)
        if resp.status_code == 403:
            raise permission_error(_response_error_message(resp), key=key, **context)
        if resp.status_code == 404:
            raise LiumNotFoundError(f"Resource not found: {_response_error_message(resp)}", **context)
        if resp.status_code == 429:
            raise LiumRateLimitError("Rate limit exceeded", **context)
        if 500 <= resp.status_code < 600:
            raise LiumServerError(f"Server error: {resp.status_code}", **context)
        raise LiumError(f"API error {resp.status_code}: {_response_error_message(resp)}", **context)

    def _dict_to_backup_config(self, config_dict: Dict) -> BackupConfig:
        """Convert backup config dict to BackupConfig object."""
        return BackupConfig(
            id=config_dict.get("id", ""),
            huid=generate_huid(config_dict.get("id", "")),
            pod_executor_id=config_dict.get("pod_executor_id", ""),
            backup_frequency_hours=config_dict.get("backup_frequency_hours", 0),
            retention_days=config_dict.get("retention_days", 0),
            backup_path=config_dict.get("backup_path", ""),
            is_active=config_dict.get("is_active", True),
            created_at=config_dict.get("created_at", ""),
            updated_at=config_dict.get("updated_at")
        )

    def _dict_to_backup_log(self, log_dict: Dict) -> BackupLog:
        """Convert backup log dict to BackupLog object."""
        return BackupLog(
            id=log_dict.get("id", ""),
            huid=generate_huid(log_dict.get("id", "")),
            backup_config_id=log_dict.get("backup_config_id", ""),
            status=log_dict.get("status", "unknown"),
            started_at=log_dict.get("started_at", ""),
            completed_at=log_dict.get("completed_at"),
            error_message=log_dict.get("error_message"),
            progress=log_dict.get("progress"),
            backup_volume_id=log_dict.get("backup_volume_id"),
            created_at=log_dict.get("created_at"),
            stage=log_dict.get("stage"),
            total_files=log_dict.get("total_files"),
            processed_files=log_dict.get("processed_files"),
            total_bytes=log_dict.get("total_bytes"),
            processed_bytes=log_dict.get("processed_bytes"),
            deletion_state=log_dict.get("deletion_state"),
            physical_cleanup_at=log_dict.get("physical_cleanup_at"),
            status_message=log_dict.get("status_message"),
            elapsed_seconds=log_dict.get("elapsed_seconds"),
            throughput_bytes_per_second=log_dict.get("throughput_bytes_per_second"),
            estimated_remaining_seconds=log_dict.get("estimated_remaining_seconds"),
        )

    def _dict_to_restore_log(self, log_dict: Dict) -> RestoreLog:
        """Convert restore log dict to RestoreLog object."""
        return RestoreLog(
            id=log_dict.get("id", ""),
            huid=generate_huid(log_dict.get("id", "")),
            backup_id=log_dict.get("backup_id", ""),
            pod_id=log_dict.get("pod_id", ""),
            status=log_dict.get("status", "unknown"),
            progress=log_dict.get("progress", 0),
            started_at=log_dict.get("started_at"),
            completed_at=log_dict.get("completed_at"),
            error_message=log_dict.get("error_message"),
            logs=log_dict.get("logs"),
            restore_path=log_dict.get("restore_path"),
            created_at=log_dict.get("created_at", ""),
            backup_engine=log_dict.get("backup_engine"),
            restore_mode=log_dict.get("restore_mode"),
            stage=log_dict.get("stage"),
            last_heartbeat_at=log_dict.get("last_heartbeat_at"),
            total_files=log_dict.get("total_files"),
            processed_files=log_dict.get("processed_files"),
            total_bytes=log_dict.get("total_bytes"),
            processed_bytes=log_dict.get("processed_bytes"),
            elapsed_seconds=log_dict.get("elapsed_seconds"),
            throughput_bytes_per_second=log_dict.get("throughput_bytes_per_second"),
            estimated_remaining_seconds=log_dict.get("estimated_remaining_seconds"),
        )

    def _dict_to_volume_info(self, volume_dict: Dict) -> VolumeInfo:
        """Convert volume dict to VolumeInfo object."""
        return VolumeInfo(
            id=volume_dict.get("id", ""),
            huid=generate_huid(volume_dict.get("id", "")),
            name=volume_dict.get("name", ""),
            description=volume_dict.get("description", ""),
            created_at=volume_dict.get("created_at", ""),
            updated_at=volume_dict.get("updated_at"),
            current_size_bytes=volume_dict.get("current_size_bytes", 0),
            current_file_count=volume_dict.get("current_file_count", 0),
            current_size_gb=volume_dict.get("current_size_gb", 0.0),
            current_size_mb=volume_dict.get("current_size_mb", 0.0),
            last_metrics_update=volume_dict.get("last_metrics_update")
        )

    def _dict_to_executor_info(self, executor_dict: Dict) -> Optional[ExecutorInfo]:
        """Convert executor dict to ExecutorInfo object."""
        if not executor_dict:
            return None

        # Extract GPU info from specs or machine_name
        specs = executor_dict.get("specs") or {}
        gpu_info = specs.get("gpu") or {}
        gpu_details = gpu_info.get("details") or []
        # The top-level gpu_count is the Executor.gpu_count column the rent path
        # multiplies price_per_gpu by, so it comes first; then the count in the
        # scraped specs, then the listed GPUs. Only assume a single GPU when the
        # API gives us nothing at all, so a missing count cannot silently turn an
        # 8-GPU node into a "1×" line with a 1-GPU price. Counts arrive as
        # strings in some payloads (see _pod_gpu_count), so each is parsed.
        gpu_count = (
            _int_or_none(executor_dict, "gpu_count")
            or _int_or_none(gpu_info, "count")
            or len(gpu_details)
            or 1
        )

        # Extract GPU type from machine_name or specs
        machine_name = executor_dict.get("machine_name") or ""
        gpu_type = extract_gpu_type(machine_name)

        # If we couldn't extract from machine_name (empty, blank, or no known pattern), try specs
        words = machine_name.split()
        unresolved = not words or gpu_type == words[-1]
        if unresolved and gpu_details:
            gpu_name = (gpu_details[0] or {}).get("name", "")
            if gpu_name:
                gpu_type = extract_gpu_type(gpu_name)

        price_per_gpu = executor_dict.get("price_per_gpu") or 0
        price_per_hour = price_per_gpu * gpu_count

        return ExecutorInfo(
            id=executor_dict.get("id", ""),
            ip=executor_dict.get("executor_ip_address", ""),
            huid=generate_huid(executor_dict.get("id", "")),
            machine_name=machine_name,
            gpu_type=gpu_type,
            gpu_count=gpu_count,
            price_per_hour=price_per_hour,
            price_per_gpu=price_per_gpu,
            location=executor_dict.get("location", {}),
            specs=specs,
            status=executor_dict.get("status", "unknown"),
            docker_in_docker=specs.get("sysbox_runtime", False),
            available_port_count=specs.get("available_port_count"),
            effective_upload_speed_mbps=executor_dict.get("effective_upload_speed_mbps"),
            effective_download_speed_mbps=executor_dict.get("effective_download_speed_mbps"),
            max_cuda_version=executor_dict.get("max_cuda_version"),
            tier=executor_dict.get("tier"),
            available_gpu_count=_int_or_none(executor_dict, "available_gpu_count"),
        )

    def list_ssh_keys(self) -> List[SSHKey]:
        """Return SSH keys registered for the current user."""
        data = self._request("GET", "/ssh-keys").json()
        if not isinstance(data, list):
            return []
        return [
            SSHKey(
                id=str(row.get("id", "")),
                name=row.get("name", ""),
                public_key=row.get("public_key", ""),
                created_at=row.get("created_at"),
            )
            for row in data
            if isinstance(row, dict)
        ]

    def register_ssh_key(self, *, name: str, public_key: str) -> SSHKey:
        """Register a new SSH public key under the current user."""
        payload = {"name": name, "public_key": public_key}
        data = self._request("POST", "/ssh-keys", json=payload).json()
        if not isinstance(data, dict):
            data = {}
        return SSHKey(
            id=str(data.get("id", "")),
            name=data.get("name", name),
            public_key=data.get("public_key", public_key),
            created_at=data.get("created_at"),
        )

    @staticmethod
    def default_ssh_key_name() -> str:
        """``cli-<user>@<host>`` sanitised to ``[A-Za-z0-9._@-]``."""
        user = getpass.getuser() or "user"
        host = socket.gethostname() or "host"
        return re.sub(r"[^A-Za-z0-9._@-]", "-", f"cli-{user}@{host}")[:64]

    def _ensure_ssh_keys_registered(
        self,
        public_keys: List[str],
        name: Optional[str] = None,
    ) -> None:
        """Make sure each pubkey in ``public_keys`` is registered server-side.

        Lazy + cached: skips the network call when every fingerprint is already
        in ``~/.lium/ssh_keys_cache.json``. On any registration failure we warn
        and return — the rent that follows must never be blocked by this step.
        """
        if not public_keys:
            return

        fps = {pk: fingerprint(pk) for pk in public_keys if pk.strip()}
        cached = load_cache(self.config)
        missing_locally = [pk for pk, fp in fps.items() if fp not in cached]
        if not missing_locally:
            return

        try:
            server_keys = {k.public_key.strip() for k in self.list_ssh_keys() if k.public_key}
        except LiumError as exc:
            warnings.warn(
                f"lium: could not list ssh-keys ({exc}); skipping registration",
                stacklevel=2,
            )
            return

        new_fps = set(cached)
        default_name = name or self.default_ssh_key_name()

        for pk in missing_locally:
            stripped = pk.strip()
            if stripped in server_keys:
                new_fps.add(fps[pk])
                continue
            try:
                self.register_ssh_key(name=default_name, public_key=stripped)
                new_fps.add(fps[pk])
            except LiumError as exc:
                warnings.warn(
                    f"lium: could not register ssh key ({exc}); continuing rent",
                    stacklevel=2,
                )

        if new_fps != cached:
            try:
                save_cache(self.config, new_fps)
            except OSError as exc:
                warnings.warn(f"lium: could not write ssh-keys cache ({exc})", stacklevel=2)

    def up(
        self,
        *,
        executor_id: str,
        name: str = "Your Pod",
        template_id: Optional[str] = None,
        dockerfile_content: Optional[str] = None,
        volume_id: Optional[str] = None,
        ports: Optional[int] = None,
        ssh_keys: Optional[List[str]] = None,
        ssh_name: Optional[str] = None,
        enable_volume_encryption: bool | None = True,
        backup_id: Optional[str] = None,
        restore_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a new pod on a specific node.

        Args:
            executor_id: Target node ID string.
            name: Human-friendly pod name (defaults to ``"Your Pod"``).
            template_id: Template ID. Defaults to the node's default template.
                Mutually exclusive with ``dockerfile_content``.
            dockerfile_content: Raw Dockerfile text to build the pod image from on
                the node (custom build). Mutually exclusive with ``template_id`` —
                pass exactly one. The image is built remotely with no network
                access, so the Dockerfile must be self-contained (no ``ADD <url>``
                or ``ADD ${var}`` directives).
            volume_id: Optional volume ID to attach on spawn.
            ports: Number of exposed ports to request.
            ssh_keys: SSH public keys to authorize. Defaults to the keys discovered by the Config.
            ssh_name: Optional name to use when registering a new SSH key with the
                backend. Defaults to ``cli-<user>@<hostname>``. Only applied to keys
                that are not already registered server-side.
            enable_volume_encryption: Whether to request encryption for the local
                pod volume. Enabled by default. The image must support Lium volume
                encryption.
            backup_id: Optional backup ID to restore after the pod starts.
            restore_path: New or empty subdirectory where the backup is restored.
                Required when ``backup_id`` is provided.

        Returns:
            Pod metadata as returned by the rent API (id, name, status, ssh command, etc.).
        """
        if template_id is not None and dockerfile_content is not None:
            raise ValueError(
                "Provide either template_id or dockerfile_content, not both"
            )
        if bool(backup_id) != bool(restore_path):
            raise ValueError("backup_id and restore_path must be provided together")

        executor_info = self.get_executor(executor_id)
        if not executor_info:
            raise ValueError(f"Node with ID '{executor_id}' not found")

        if template_id is None and dockerfile_content is None:
            selected_template = self.default_docker_template(executor_info.id)
            template_id = selected_template.id

        ssh_material = ssh_keys or self.config.ssh_public_keys
        if not ssh_material:
            raise ValueError("No SSH keys found")

        self._ensure_ssh_keys_registered(ssh_material, name=ssh_name)

        payload = {
            "pod_name": name,
            "template_id": template_id,
            "dockerfile_content": dockerfile_content,
            "volume_id": volume_id,
            "user_public_key": ssh_material,
            "initial_port_count": ports,
            "enable_volume_encryption": enable_volume_encryption,
            "backup_log_id": backup_id,
            "restore_path": restore_path,
        }

        # The rent call is not idempotent, so it is never retried blindly. A
        # timeout or a 5xx may have created the pod anyway; look for it before
        # sending the request a second time. The Idempotency-Key lets a server
        # that honours it collapse the two requests; one that does not ignores it.
        rent_endpoint = f"/executors/{executor_info.id}/rent"
        rent_headers = {**self.headers, "Idempotency-Key": str(uuid.uuid4())}
        # Pods that exist before the rent can never be the one this call created.
        # With GPU splitting a node hosts several pods, and the default pod name
        # is the node huid, so a stale same-name pod on the same node would
        # otherwise be handed back for a rent the server never received.
        known_pod_ids = self._pod_ids_before_rent()
        try:
            response = self._request(
                "POST", rent_endpoint, json=payload, headers=rent_headers, retry=False
            ).json()
        except (requests.RequestException, LiumServerError, LiumRateLimitError):
            # "Could not look" (the network that failed the POST fails ps() the
            # same way) must fall through to the second POST, not escape here.
            existing = self._find_pod_by_name(
                name, executor_info.id, attempts=3, interval=3, exclude=known_pod_ids
            )
            if existing:
                return existing
            time.sleep(1)
            response = self._request(
                "POST", rent_endpoint, json=payload, headers=rent_headers, retry=False
            ).json()

        # API should return pod info
        if response and "id" in response:
            return response

        # The rent route answers {"success": true, "pod_id": ...}: the id is
        # exact, so the pod is read back by it rather than guessed by name. If
        # the listing cannot be read, the id alone is still the truth: the pod
        # exists, and the caller gets its id rather than an error.
        pod_id = (response or {}).get("pod_id")
        if pod_id:
            existing = self._find_pod_by_id(str(pod_id), executor_info.id, attempts=3, interval=2)
            return existing or {
                "id": str(pod_id),
                "name": name,
                "status": "PENDING",
                "huid": generate_huid(str(pod_id)),
                "ssh_cmd": None,
                "executor_id": executor_info.id,
            }

        # Fallback: find pod by name after creation
        existing = self._find_pod_by_name(
            name, executor_info.id, attempts=2, interval=3, exclude=known_pod_ids
        )
        if existing:
            return existing

        raise LiumError(f"Failed to create pod{' ' + name if name else ''}")

    def _list_pods_or_none(self) -> Optional[List[PodInfo]]:
        """``ps()`` for the rent lookups: ``None`` when the listing itself failed,
        so a network that is down for the POST is not mistaken for "no pod"."""
        try:
            return self.ps()
        except (requests.RequestException, LiumError):
            return None

    def _find_pod_by_id(
        self, pod_id: str, executor_id: str, *, attempts: int, interval: float
    ) -> Optional[Dict[str, Any]]:
        """The pod ``pod_id`` from ``ps``; the server already committed it, so the
        listing is read at once and only re-read if the row is not there yet."""
        for attempt in range(attempts):
            if attempt:
                time.sleep(interval)
            for pod in self._list_pods_or_none() or []:
                if pod.id == pod_id:
                    return self._created_pod_record(pod, executor_id)
        return None

    def _pod_ids_before_rent(self) -> frozenset:
        """Ids of the pods that exist right now, taken before a rent is sent.

        A listing failure of any kind must not turn into a failed ``up``; an
        empty snapshot only means the by-name lookup cannot rule out older pods.
        """
        try:
            return frozenset(pod.id for pod in self.ps())
        except Exception:  # noqa: BLE001 - best effort by design
            return frozenset()

    def _find_pod_by_name(
        self,
        name: Optional[str],
        executor_id: str,
        *,
        attempts: int,
        interval: float,
        exclude: frozenset = frozenset(),
    ) -> Optional[Dict[str, Any]]:
        """The dict :meth:`up` returns for a pod called ``name`` on ``executor_id``, if one
        shows up in ``ps`` (see :meth:`_find_pod_info_by_name`)."""
        pod = self._find_pod_info_by_name(name, executor_id, attempts=attempts, interval=interval, exclude=exclude)
        return None if pod is None else self._created_pod_record(pod, executor_id)

    def _find_pod_info_by_name(
        self,
        name: Optional[str],
        executor_id: Optional[str],
        *,
        attempts: int,
        interval: float,
        exclude: frozenset = frozenset(),
    ) -> Optional[PodInfo]:
        """A pod called ``name`` if one shows up in ``ps``.

        Used when the rent response did not say what it created. With ``executor_id`` the
        executor is matched when the listing includes one, so two pods sharing a generic
        name on different nodes are not confused; ``None`` (a rent-by-spec, where the server
        chose the node) matches on the name alone. Pods whose id is in ``exclude`` (the ones
        that existed before the rent) are never returned.
        """
        if not name:
            return None
        for _ in range(attempts):
            time.sleep(interval)
            for pod in self._list_pods_or_none() or []:
                if pod.id in exclude or pod.name != name:
                    continue
                if (
                    executor_id
                    and pod.executor is not None
                    and pod.executor.id
                    and pod.executor.id != executor_id
                ):
                    continue
                return pod
        return None

    @staticmethod
    def _created_pod_record(pod: PodInfo, executor_id: str) -> Dict[str, Any]:
        """The dict :meth:`up` returns for a pod read back from the listing."""
        return {
            "id": pod.id,
            "name": pod.name,
            "status": pod.status,
            "huid": pod.huid,
            "ssh_cmd": pod.ssh_cmd,
            "executor_id": executor_id,
        }

    def rent(
        self,
        *,
        gpu_type: str,
        gpu_count: int = 1,
        name: str = "Your Pod",
        template_id: Optional[str] = None,
        dockerfile_content: Optional[str] = None,
        min_vram_gb: Optional[float] = None,
        min_cpus: Optional[int] = None,
        min_ram_gb: Optional[float] = None,
        min_disk_gb: Optional[float] = None,
        min_download_mbps: Optional[float] = None,
        min_ports: Optional[int] = None,
        max_price_per_gpu_hour: Optional[float] = None,
        country: Optional[str] = None,
        docker_in_docker: Optional[bool] = None,
        interconnect: Optional[str] = None,
        volume_id: Optional[str] = None,
        ports: Optional[int] = None,
        ssh_keys: Optional[List[str]] = None,
        ssh_name: Optional[str] = None,
        enable_volume_encryption: bool | None = True,
        backup_id: Optional[str] = None,
        restore_path: Optional[str] = None,
        dry_run: bool = False,
    ) -> RentResult:
        """Rent the cheapest available node that satisfies a spec, without listing the fleet.

        When the backend advertises ``rent_by_spec`` (``GET /version``), one call to
        ``POST /executors/rent-by-spec`` selects and rents: the server picks the cheapest
        ``$/GPU·h`` node that meets every constraint (ties: faster ingress, then reliability,
        then id), rents it under its own locks and, if that node is taken meanwhile, tries the
        next candidate. Against an older backend the same arguments make today's client-side
        pick — list, filter, cheapest exact match — and rent it with :meth:`up`.

        Args:
            gpu_type: Short or full GPU name — ``"H100"``, ``"RTX4090"``, ``"NVIDIA H200"``.
            gpu_count: GPUs to rent (default 1). Server-side this also admits a split of a
                larger node when its provider allows one; client-side it is the node's size.
            name: Pod name.
            template_id: Template to run. Omitted: the node's recommended image.
                Mutually exclusive with ``dockerfile_content``.
            dockerfile_content: Build the image from this Dockerfile instead (see :meth:`up`).
            min_vram_gb, min_cpus, min_ram_gb, min_disk_gb, min_download_mbps, min_ports:
                Floors on the host; a host that does not report the figure does not qualify.
            max_price_per_gpu_hour: Ceiling on ``price_per_gpu``.
            country: ISO country code.
            docker_in_docker: Require a sysbox host.
            interconnect: ``"nvlink"`` — every GPU pair on NVLink (unreported counts as no).
                Client-side only: a rent-by-spec backend has no such field, so the call is
                refused there rather than sent with a constraint the server would not check.
            volume_id, ports, ssh_keys, ssh_name, enable_volume_encryption, backup_id,
                restore_path: as in :meth:`up`.
            dry_run: Choose and price only; nothing is rented and no SSH key is registered
                with the account. Against a rent-by-spec backend the request still carries
                your SSH public key (``ssh_keys`` or the configured key: the server validates
                a dry run as a rent); the client-side pick needs no key.

        Returns:
            :class:`RentResult` — the node, the GPUs rented, the hourly price, the pod (``None``
            on a dry run), the template used, how many nodes qualified and the runners-up.

        Raises:
            LiumError: No node satisfies the spec. The message names the constraint that
                left no candidate and the best value on offer, e.g.
                ``min_cpus=64: none of the 12 node(s) matching the earlier constraints
                satisfies it; the best on offer is 48``.
            LiumError: ``interconnect`` was given and the backend rents by spec (nothing is
                registered or rented).
            LiumServerError, requests.RequestException: the rent-by-spec response was lost. The
                rent is posted once (a repeat could rent a second node); the pod is looked up
                by name for ~9 s and returned when it appears, otherwise the error propagates
                and the pod may still exist — check :meth:`ps`.
        """
        if template_id is not None and dockerfile_content is not None:
            raise ValueError("Provide either template_id or dockerfile_content, not both")
        if bool(backup_id) != bool(restore_path):
            raise ValueError("backup_id and restore_path must be provided together")

        spec = {
            "gpu_type": gpu_type,
            "gpu_count": gpu_count,
            "min_vram_gb": min_vram_gb,
            "min_cpus": min_cpus,
            "min_ram_gb": min_ram_gb,
            "min_disk_gb": min_disk_gb,
            "min_download_mbps": min_download_mbps,
            "min_ports": min_ports,
            "max_price_per_gpu_hour": max_price_per_gpu_hour,
            "country": country,
            "docker_in_docker": docker_in_docker,
            "interconnect": interconnect,
        }
        spec = {key: value for key, value in spec.items() if value is not None}
        rental = {
            "name": name,
            "template_id": template_id,
            "dockerfile_content": dockerfile_content,
            "volume_id": volume_id,
            "ports": ports,
            "ssh_keys": ssh_keys,
            "ssh_name": ssh_name,
            "enable_volume_encryption": enable_volume_encryption,
            "backup_id": backup_id,
            "restore_path": restore_path,
        }
        if self.supports(RENT_BY_SPEC):
            return self._rent_on_server(spec, rental, dry_run)
        return self._rent_client_side(spec, rental, dry_run)

    def _rent_on_server(self, spec: Dict[str, Any], rental: Dict[str, Any], dry_run: bool) -> RentResult:
        # RentBySpecRequest has no `interconnect` field: the server would drop the key and rent a
        # node without checking it. Refuse before anything is registered or rented.
        if "interconnect" in spec:
            raise LiumError(
                "interconnect is not a constraint this backend's rent-by-spec accepts, so it would "
                "rent a node without checking it. Drop interconnect, or pick the node with ls() and "
                "rent it with up()."
            )
        # The server's RentBySpecRequest requires user_public_key on a dry run too (it validates
        # the request as a rent), so the key is needed either way; only the registration is skipped.
        ssh_material = rental["ssh_keys"] or self.config.ssh_public_keys
        if not ssh_material:
            raise ValueError("No SSH keys found")
        if not dry_run:
            self._ensure_ssh_keys_registered(ssh_material, name=rental["ssh_name"])

        payload = {
            **spec,
            "pod_name": rental["name"],
            "template_id": rental["template_id"],
            "dockerfile_content": rental["dockerfile_content"],
            "volume_id": rental["volume_id"],
            "user_public_key": ssh_material,
            "initial_port_count": rental["ports"],
            "enable_volume_encryption": rental["enable_volume_encryption"],
            "backup_log_id": rental["backup_id"],
            "restore_path": rental["restore_path"],
            "dry_run": dry_run,
        }
        gpu_count = int(spec.get("gpu_count") or 1)
        # A rent is billable and a lost response may have succeeded server-side, so it is sent
        # once, as in `up`; a dry run rents nothing and keeps the retries. Pods that exist before
        # the rent can never be the one it created (see `up`).
        known_pod_ids = frozenset() if dry_run else self._pod_ids_before_rent()
        try:
            data = self._request("POST", "/executors/rent-by-spec", json=payload, retry=dry_run).json()
        except (requests.RequestException, LiumServerError, LiumRateLimitError):
            if dry_run:
                raise
            # The server may have rented before the response was lost: hand back the pod it
            # created rather than an error next to a billing pod. Nothing is posted again — a
            # second rent-by-spec could pick another node — so when no pod appears the error
            # propagates and the docstring tells the caller to check `ps`.
            pod = self._find_pod_info_by_name(
                rental["name"], None, attempts=3, interval=3, exclude=known_pod_ids
            )
            if pod is None or pod.executor is None:
                raise
            return RentResult(
                executor=pod.executor,
                price_per_hour=pod.executor.price_per_hour,  # ps() anchors it on the pod's billed price
                gpu_count=gpu_count,
                pod=self._created_pod_record(pod, pod.executor.id),
                template_id=rental["template_id"],
                candidates=1,
                attempts=1,
                server_side=True,
            )
        executor = self._dict_to_executor_info(data.get("selected_executor") or {})
        if executor is None:
            raise LiumError("rent-by-spec returned no node")
        pod_id = data.get("pod_id")
        return RentResult(
            executor=executor,
            price_per_hour=float(data.get("price_per_hour") or executor.price_per_hour),
            gpu_count=gpu_count,
            pod=None
            if pod_id is None
            else {"id": pod_id, "name": rental["name"], "status": "PENDING", "executor_id": executor.id},
            template_id=data.get("template_id"),
            candidates=int(data.get("candidates") or 1),
            alternatives=list(data.get("alternatives_considered") or []),
            attempts=int(data.get("attempts") or 0),
            dry_run=bool(data.get("dry_run", dry_run)),
            server_side=True,
        )

    def _rent_client_side(self, spec: Dict[str, Any], rental: Dict[str, Any], dry_run: bool) -> RentResult:
        # Today's pick, for a backend without rent-by-spec: cheapest $/GPU·h node with exactly
        # gpu_count GPUs that meets the constraints; ties keep the faster ingress, then the id.
        executors = self.ls(gpu_type=spec["gpu_type"])
        matches = [e for e in executors if _satisfies_spec(e, spec)]
        if not matches:
            on_offer = sorted({f"{e.gpu_count}x{e.gpu_type} ${e.price_per_hour:.2f}/h" for e in executors})
            hint = f" Available: {', '.join(on_offer)}." if on_offer else ""
            wanted = ", ".join(f"{key}={value}" for key, value in spec.items())
            raise LiumError(f"No node matches {wanted}.{hint}")
        matches.sort(key=lambda e: (e.price_per_gpu or float("inf"), -e.download_speed, e.id))
        executor = matches[0]
        alternatives = [
            {
                "id": e.id,
                "machine_name": e.machine_name,
                "gpu_count": e.gpu_count,
                "price_per_gpu": e.price_per_gpu,
                "price_per_hour": e.price_per_hour,
                "download_mbps": e.download_speed,
                "country_code": (e.location or {}).get("country_code"),
            }
            for e in matches[1:6]
        ]
        pod = None
        if not dry_run:
            pod = self.up(
                executor_id=executor.id,
                name=rental["name"],
                template_id=rental["template_id"],
                dockerfile_content=rental["dockerfile_content"],
                volume_id=rental["volume_id"],
                ports=rental["ports"],
                ssh_keys=rental["ssh_keys"],
                ssh_name=rental["ssh_name"],
                enable_volume_encryption=rental["enable_volume_encryption"],
                backup_id=rental["backup_id"],
                restore_path=rental["restore_path"],
            )
        return RentResult(
            executor=executor,
            price_per_hour=executor.price_per_hour,
            gpu_count=executor.gpu_count,  # exact match on gpu_count: the whole node is rented
            pod=pod,
            template_id=rental["template_id"],
            candidates=len(matches),
            alternatives=alternatives,
            attempts=0 if dry_run else 1,
            dry_run=dry_run,
            server_side=False,
        )

    def pod(
        self,
        pod_id: str
    ) -> Dict[str, Any]:
        """Retrieve detailed information about a specific pod.

        Args:
            pod_id: The unique identifier of the pod to retrieve.

        Returns:
            Raw pod data dictionary including template, node, status, and connection info.
        """
        return self._request("GET", f"/pods/{pod_id}").json()

    def logs(
        self,
        pod_id: str,
        *,
        tail: int = 100,
        follow: bool = False,
    ) -> Generator[bytes, None, None]:
        """Stream logs from a pod.

        Args:
            pod_id: The unique identifier of the pod.
            tail: Number of lines to retrieve from the end of the logs (default: 100).
            follow: If True, stream logs continuously (default: False).

        Yields:
            Log lines as bytes.
        """
        params = {"tail": tail, "follow": str(follow).lower()}

        try:
            response = self._request(
                "GET",
                # the id comes from the caller: quoted so a stray "/" or "?" cannot point the request at another route
                f"/pods/{quote(str(pod_id), safe='')}/logs",
                params=params,
                stream=True,
                timeout=None if follow else 30,
            )
        except LiumNotFoundError as e:
            raise LiumNotFoundError(
                f"Pod not found: {pod_id}", code=e.code, hint=e.hint, request_id=e.request_id
            ) from None

        with response:
            for line in response.iter_lines():
                if line:
                    yield line

    def edit(
        self,
        pod_id: str,
        **kwargs
    ) -> Dict[str, Any]:
        """Edit a pod's template configuration.

        Updates the template associated with a pod by merging the provided
        keyword arguments with the existing template settings.

        Args:
            pod_id: The unique identifier of the pod whose template to edit.
            **kwargs: Template fields to update. Common fields include:
                - docker_image (str): Docker image repository.
                - docker_image_tag (str): Docker image tag.
                - startup_commands (str): Commands to run on container start.
                - internal_ports (List[int]): Ports to expose.
                - environment (Dict[str, str]): Environment variables.
                - volumes (List[str]): Volume mount paths.

        Returns:
            Updated template data dictionary from the API.

        Example:
            >>> lium.edit(pod_id, startup_commands="python main.py", environment={"DEBUG": "1"})
        """
        pod = self.pod(pod_id=pod_id)

        payload = {
            **pod["template"],
            **kwargs,
        }

        result = self._request("PUT", f"/templates/{pod['template']['id']}", json=payload).json()
        # The backend routes this PUT into a container reboot (pod_service.edit_pod ->
        # reboot_rental_container), so the pod's SSH host key changes with it.
        forget_host_key(pod_id)
        return result

    def ls(
        self,
        *,
        gpu_type: Optional[str] = None,
        gpu_count: Optional[int] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        max_distance_miles: Optional[int] = None,
        min_cuda_version: Optional[float] = None,
        min_cpus: Optional[int] = None,
    ) -> List[ExecutorInfo]:
        """List available nodes.

        Args:
            gpu_type: Optional GPU filter such as ``"A100"`` or ``"H200"``.
            gpu_count: Exact GPU count to match (defaults to 8, pass ``None`` to disable).
            lat: Optional latitude for geospatial filtering. Must be used together with ``lon`` and ``max_distance_miles``.
            lon: Optional longitude for geospatial filtering. Must be used together with ``lat`` and ``max_distance_miles``.
            max_distance_miles: Optional radius (in miles) for geospatial filtering. Must be used together with ``lat`` and ``lon``.
            min_cuda_version: Optional minimum CUDA version to require (e.g. ``12.4``). Nodes whose
                ``max_cuda_version`` is ``None`` or below this threshold are excluded. NVIDIA drivers are
                backward compatible, so a node with a higher driver CUDA version satisfies the requirement.
            min_cpus: Optional minimum CPU thread count (``specs.cpu.count``). Nodes that report fewer
                CPUs, or none, are excluded.

        Returns:
            A list of :class:`ExecutorInfo` objects that satisfy the filters.
        """
        params: Dict[str, Any] = {"size": 1000}
        if gpu_type:
            # Try to map short GPU name to full machine name
            machine_name = self._resolve_machine_name(gpu_type)
            if machine_name:
                params["machine_names"] = machine_name
            else:
                # If no match found, use the input as-is (might be already a full name)
                params["machine_names"] = gpu_type
        if gpu_count:
            params["gpu_count_gte"] = gpu_count
            params["gpu_count_lte"] = gpu_count
        if lat is not None and lon is not None:
            params["lat"] = lat
            params["lon"] = lon
            if max_distance_miles is not None:
                params["max_distance_mile"] = max_distance_miles
        elif max_distance_miles is not None:
            params["max_distance_mile"] = max_distance_miles

        data = self._request("GET", "/executors", params=params).json()
        executors = [self._dict_to_executor_info(d) for d in data]
        executors = [e for e in executors if e]  # Filter None values

        if min_cuda_version is not None:
            executors = [
                e for e in executors
                if e.max_cuda_version is not None and e.max_cuda_version >= min_cuda_version
            ]

        if min_cpus is not None:
            executors = [e for e in executors if e.cpu_count is not None and e.cpu_count >= min_cpus]

        return executors

    def ps(self) -> List[PodInfo]:
        """List active pods.

        Returns:
            List of :class:`PodInfo` objects representing the caller's running pods.
        """
        data = self._request("GET", "/pods").json()

        pods = []
        for d in data:
            executor = self._dict_to_executor_info(d.get("executor") or {}) if d.get("executor") else None
            # The /pods endpoint returns the authoritative total $/h as pod.price; the
            # nested executor.price_per_gpu is not populated in this payload. Anchor
            # executor.price_per_hour on pod.price and derive per-GPU from it. The
            # executor describes the WHOLE host and stays so; for a GPU-split rental
            # (1 GPU of a 3×3090 node) the pod row's own gpu_count is the billed count,
            # so per-GPU is pod.price over that count when the API sent one.
            pod_price = d.get("price")
            pod_gpu_count = _pod_gpu_count(d)
            if executor is not None and pod_price is not None:
                executor.price_per_hour = float(pod_price)
                executor.price_per_gpu = float(pod_price) / max(1, pod_gpu_count or executor.gpu_count)
            pods.append(PodInfo(
                id=d.get("id", ""),
                name=d.get("pod_name", ""),
                status=d.get("status", "unknown"),
                huid=generate_huid(d.get("id", "")),
                ssh_cmd=d.get("ssh_connect_cmd"),
                ports=d.get("ports_mapping", {}),
                created_at=d.get("created_at", ""),
                updated_at=d.get("updated_at", ""),
                executor=executor,
                template=d.get("template", {}),
                removal_scheduled_at=d.get("removal_scheduled_at"),
                jupyter_installation_status=d.get("jupyter_installation_status"),
                jupyter_url=d.get("jupyter_url"),
                enable_volume_encryption=d.get("enable_volume_encryption"),
                volume_encryption_status=d.get("volume_encryption_status"),
                estimated_ready_seconds=d.get("estimated_ready_seconds"),
                eta_basis=d.get("eta_basis"),
                phase=d.get("phase"),
                gpu_count=pod_gpu_count,
                workspace_id=d.get("workspace_id"),
            ))

        return pods

    def down(self, pod: PodInfo) -> Dict[str, Any]:
        """Stop a pod.

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        result = self._request("DELETE", f"/pods/{pod.id}").json()
        forget_host_key(pod)
        return result

    def rm(self, pod: PodInfo) -> Dict[str, Any]:
        """Remove pod (alias for :meth:`down`).

        Args:
            pod: Pod to terminate.

        Returns:
            API response payload from the delete call.
        """
        return self.down(pod)

    def reboot(self, pod: PodInfo, volume_id: Optional[str] = None) -> Dict[str, Any]:
        """Reboot a pod.

        Args:
            pod: Pod to reboot.
            volume_id: Optional volume ID to attach for the reboot request.

        Returns:
            Pod data from the API response after issuing the reboot.
        """
        payload: Dict[str, Optional[str]] = {}
        if volume_id is not None:
            payload["volume_id"] = volume_id

        result = self._request("POST", f"/pods/{pod.id}/reboot", json=payload or {}).json()
        # The reboot replaces the container and with it the SSH host key; the next
        # connection re-pins rather than tripping over the old key.
        forget_host_key(pod)
        return result

    def get_default_images(self, gpu_model: Optional[str], driver_version: Optional[str]) -> list[dict]:
        """Get default images for GPU type and driver version."""
        params = {
            "gpu_model": gpu_model,
            "driver_version": driver_version
        }
        data = self._request("GET", "/executors/default-docker-image", params=params).json()
        return data

    def _select_fallback_template(self) -> Optional[Template]:
        """Pick a reasonable default template (prefer PyTorch, else first available)."""
        templates = self.templates()
        if not templates:
            return None

        def is_pytorch(template: Template) -> bool:
            category = (template.category or "").upper()
            image = (template.docker_image or "").lower()
            return "PYTORCH" in category or "pytorch" in image

        pytorch_templates = [t for t in templates if is_pytorch(t)]

        if pytorch_templates:
            def version_key(template: Template):
                tag = template.docker_image_tag or ""
                version_part = tag.split('-')[0]
                parts = []
                for piece in version_part.split('.'):
                    if piece.isdigit():
                        parts.append(int(piece))
                    else:
                        break
                return tuple(parts)

            return max(pytorch_templates, key=version_key)

        return templates[0]

    def default_docker_template(self, executor_id: str) -> Template:
        """Resolve the best default template for a node ID.

        Args:
            executor_id: Node identifier returned by :meth:`ls`.

        Returns:
            :class:`Template` best suited for the node.

        Raises:
            ValueError: If no matching node or template exists.
        """
        executor = self.get_executor(executor_id)
        if not executor:
            raise ValueError(f"No node found with id {executor_id}")

        default_images = self.get_default_images(executor.gpu_model, executor.driver_version)

        pytorch_image = next(
            (img for img in default_images if "pytorch" in img.get("docker_image", "").lower()), None
        )
        # set pytorch_image as first image
        if pytorch_image:
            default_images = [pytorch_image] + default_images
        for img in default_images:
            template = self.get_template_by_image_name(img.get("docker_image"), img.get("docker_image_tag"))
            if template:
                return template

        fallback = self._select_fallback_template()
        if fallback:
            return fallback

        raise ValueError("No templates available to use for node")


    def templates(self, filter: Optional[str] = None, only_my: bool = False) -> List[Template]:
        """List available templates.

        Args:
            filter: Optional substring to filter by image or name.
            only_my: When ``True`` return only templates owned by the caller.

        Returns:
            List of :class:`Template`.
        """
        data = self._request("GET", "/templates").json()

        if only_my:
            user_id = self.get_my_user_id()
            data = [d for d in data if d.get("user_id") == user_id]

        templates = [
            Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
            for d in data
        ]
        if filter:
            filter_lower = filter.lower()
            templates = [
                t for t in templates
                if filter_lower in t.docker_image.lower() or filter_lower in t.name.lower()
            ]

        return templates


    def get_executor(self, executor: str) -> Optional[ExecutorInfo]:
        """Resolve a node by ID.

        Args:
            executor: Node ID string.

        Returns:
            Matching :class:`ExecutorInfo` or ``None`` if not found.
        """
        for e in self.ls():
            if e.id == executor:
                return e
        return None

    def _resolve_machine_name(self, gpu_short: str) -> Optional[str]:
        """Resolve a short GPU name to all matching full machine names from API.

        Args:
            gpu_short: Short GPU name like "A100", "H200", etc.

        Returns:
            Comma-separated string of all matching machine names, or None if not found.
        """
        try:
            available_machines = self._request("GET", "/machines").json()
            matching_machines = []

            for machine in available_machines:
                machine_name = machine.get("name", "")
                # Both sides go through normalize_gpu_short inside gpu_short_matches: pattern hits
                # are already upper-case, but a name with no pattern hit keeps its casing ("Ti", "Xp"),
                # and `--gpu ti` must still find it; a bare "4090" names RTX4090 — the form users type most.
                if gpu_short_matches(gpu_short, extract_gpu_type(machine_name)):
                    matching_machines.append(machine_name)

            # Return comma-separated list of all matches
            if matching_machines:
                return ",".join(matching_machines)
        except Exception:
            pass
        return None

    def gpu_types(self) -> set[str]:
        """Get list of available GPU types.

        Returns:
            Set of GPU type strings advertised by the API.
        """
        available_machines = self._request("GET", "/machines").json()
        gpu_types = {machine.get("name") or "" for machine in available_machines}
        return gpu_types

    def gpu_short_types(self) -> List[str]:
        """The short GPU types the marketplace knows (``H100``, ``RTX4090``, ...), sorted.

        These are the values ``--gpu`` / ``ls(gpu_type=)`` accept; a bare model number
        (``4090``) and spacing/case variants (``rtx 4090``) resolve to them too. Names
        the extractor could not type (it falls back to the last word: ``Ti``, ``SUPER``,
        ``V``) are left out — they are not something ``--gpu`` can usefully take.
        """
        types = {extract_gpu_type(name) for name in self.gpu_types() if name}
        return sorted(t for t in types if re.fullmatch(r"[A-Z]*\d{2,4}[A-Z]*", t))

    def unknown_gpu_type(self, gpu_short: str) -> Optional[List[str]]:
        """``None`` when ``gpu_short`` names a known GPU type; otherwise the list of known types.

        Lets a caller tell "every 4090 is rented" from "nothing is called 4090" — the
        second case is what a typo or an unsupported spelling produces, and the two need
        different messages. Never raises: on an API failure the answer is ``None``
        (assume known), so a listing failure is reported as such and not as a typo.
        """
        try:
            names = [name for name in self.gpu_types() if name]
        except (LiumError, requests.RequestException, ValueError):
            # the listing failed (API error, transport, or a non-JSON body): assume known — the
            # caller reports the listing failure it already has, not a typo
            return None
        if not names:
            # an empty marketplace has no types to show back — "Types on the marketplace:" with
            # nothing after it would read as a typo; the caller's rented-out/no-match message fits
            return None
        # "known" is decided the way _resolve_machine_name matches — an exact catalog name first
        # (`ls()` falls back to passing the full machine name, and tab completion offers exactly
        # those), then every extracted type, fall-through spellings included (`ti`, `xp`) — so a
        # spelling the listing resolves is never called a typo; the list shown back is the typed
        # one gpu_short_types keeps (the fall-through words are not something --gpu can usefully take).
        if gpu_short in names or any(gpu_short_matches(gpu_short, extract_gpu_type(name)) for name in names):
            return None
        return sorted({t for t in (extract_gpu_type(n) for n in names) if re.fullmatch(r"[A-Z]*\d{2,4}[A-Z]*", t)})

    def get_template(self, template_id: str) -> Optional[Template]:
        """Fetch a template by ID/HUID/name.

        Args:
            template_id: Template ID, HUID, or name to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        try:
            d = self._request("GET", f"/templates/{template_id}").json()
            return Template(
                id=d.get("id", ""),
                huid=generate_huid(d.get("id", "")),
                name=d.get("name", ""),
                docker_image=d.get("docker_image", ""),
                docker_image_tag=d.get("docker_image_tag", "latest"),
                category=d.get("category", "general"),
                status=d.get("status", "unknown"),
            )
        except Exception:
            return None

    def get_template_by_image_name(self, image_name: Optional[str] = None, image_tag: Optional[str] = None) -> Optional[Template]:
        """Fetch a template by its Docker image + tag.

        Args:
            image_name: Repository/image name.
            image_tag: Tag to match.

        Returns:
            Matching :class:`Template` or ``None`` if not found.
        """
        templates = self.templates()
        for t in templates:
            if t.docker_image == image_name and t.docker_image_tag == image_tag:
                return t

    @contextmanager
    def ssh_connection(self, pod: PodInfo, timeout: int = 30):
        """SSH connection context manager.

        Args:
            pod: Pod whose SSH metadata is used.
            timeout: Connection timeout in seconds.

        Yields:
            An active ``paramiko.SSHClient``.
        """
        held = self._ssh_sessions.get(pod.id)
        if held is not None:
            yield held
            return

        if not pod.ssh_cmd:
            raise ValueError(f"No SSH for pod {pod.name}")

        if not self.config.ssh_key_path:
            raise ValueError("No SSH key configured")

        # Parse SSH command
        parts = shlex.split(pod.ssh_cmd)
        user_host = parts[1]
        user, host = user_host.split("@")
        port = pod.ssh_port

        # Load SSH key
        key = None
        for key_type in [paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey]:
            try:
                key = key_type.from_private_key_file(str(self.config.ssh_key_path))
                break
            except (paramiko.SSHException, FileNotFoundError, PermissionError):
                continue

        # Connect. Host keys are pinned per pod under ~/.lium/known_hosts/ (trust
        # on first use, reject on change) unless LIUM_SSH_INSECURE=1.
        client = paramiko.SSHClient()
        if ssh_insecure():
            client.set_missing_host_key_policy(_InsecureAcceptPolicy())
        else:
            hosts_file = known_hosts_path(pod)
            _ensure_known_hosts_file(hosts_file)
            client.load_host_keys(str(hosts_file))
            client.set_missing_host_key_policy(_PinOnFirstUsePolicy())
        connect_kwargs = {
            "hostname": host,
            "port": port,
            "username": user,
            "timeout": timeout,
            "look_for_keys": False,
        }
        if key:
            connect_kwargs["pkey"] = key
            connect_kwargs["allow_agent"] = False
        else:
            # System ssh can still work for encrypted keys via ssh-agent even when
            # Paramiko cannot parse the private key file directly.
            connect_kwargs["key_filename"] = str(self.config.ssh_key_path)
            connect_kwargs["allow_agent"] = True
        try:
            client.connect(**connect_kwargs)
        except paramiko.BadHostKeyException as e:
            hosts_file = known_hosts_path(pod)
            raise LiumHostKeyError(
                f"Host key for pod {pod.name} ({host}:{port}) changed: got "
                f"{host_key_fingerprint(e.key)}, pinned "
                f"{host_key_fingerprint(e.expected_key)}. The container, and its key, were "
                f"replaced: a reboot or template change made outside this SDK, or the platform "
                f"restarting the pod on its own (it retries failed pods); it can also mean the "
                f"connection is being intercepted. If you trust the new key, delete {hosts_file} "
                f"and reconnect; {_SSH_INSECURE_ENV}=1 disables pinning."
            ) from e

        try:
            yield client
        finally:
            client.close()

    @staticmethod
    def _env_exports(env: Dict[str, str]) -> str:
        """``export NAME=value`` statements for ``env``, shell-quoted so each value
        reaches the pod byte-for-byte (spaces, quotes, ``$``, newlines).

        Names must be valid shell identifiers (DAH-2894); anything else raises
        :class:`ValueError` here rather than failing with an opaque
        ``export: not a valid identifier`` on the pod.
        """
        exports = []
        for name, value in env.items():
            if not ENV_NAME.fullmatch(name):
                raise ValueError(
                    f"Invalid environment variable name {name!r}: use letters, digits and "
                    "underscores, not starting with a digit"
                )
            exports.append(f"export {name}={shlex.quote(str(value))}")
        return " && ".join(exports)

    # Read the exports from stdin and evaluate them in the remote shell. Nothing
    # here names a value, so the pod's argv never carries one.
    _ENV_FROM_STDIN = 'eval "$(cat)"'

    @contextmanager
    def ssh_session(self, pod: PodInfo, timeout: int = 30):
        """Keep one SSH connection to ``pod`` open for the whole block.

        Every :meth:`exec`, :meth:`stream_exec`, :meth:`upload` and :meth:`download`
        inside it runs over this connection instead of paying a fresh TCP + SSH
        handshake each (several seconds per call to a distant node).

        Yields:
            The active ``paramiko.SSHClient``.
        """
        with self.ssh_connection(pod, timeout) as client:
            self._ssh_sessions[pod.id] = client
            try:
                yield client
            finally:
                self._ssh_sessions.pop(pod.id, None)

    def _prep_command(self, command: str, env: Optional[Dict[str, str]] = None) -> str:
        """Prefix ``command`` with the exports spelled out inline.

        The values end up in the remote argv, so this form is for pty transports
        only: :meth:`stream_exec` requests a pty, which echoes stdin back into the
        output and never delivers the client's EOF, so the exports cannot travel
        the way :meth:`exec` sends them. Anything that runs through
        :meth:`exec` (a detached launcher, a background job) passes ``env=`` to
        it instead — the launching shell exports over stdin and its children
        inherit the environment without a value ever reaching ``ps``.
        """
        if env:
            return f"{self._env_exports(env)} && {command}"
        return command

    def exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Execute a shell command on a pod over SSH.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.
                The values are sent over the session's stdin, not in the remote
                command line, so ``ps`` on the pod never shows them.

        Returns:
            Dict containing stdout, stderr, exit_code, and success flag.

        Raises:
            ValueError: an ``env`` name is not a shell identifier. Raised before
                the connection is opened, so nothing reaches the pod.
        """
        # Build (and so name-check) the exports first: once ``eval "$(cat)" && cmd``
        # has been sent, a failure here would leave ``cmd`` running with no env.
        exports = self._env_exports(env) if env else ""
        if exports:
            command = f"{self._ENV_FROM_STDIN} && {command}"

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command)
            if exports:
                stdin.write(exports.encode("utf-8"))
            # Send EOF: a remote command that reads stdin waits forever otherwise,
            # and this call has no stdin to give it.
            stdin.close()
            exit_code = stdout.channel.recv_exit_status()
            return {
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace"),
                "exit_code": exit_code,
                "success": exit_code == 0
            }

    def stream_exec(
        self,
        pod: PodInfo,
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
        pty: bool = True,
    ) -> Generator[Dict[str, str], None, int]:
        """Execute a shell command and stream incremental output.

        Args:
            pod: Pod to target.
            command: Shell command to run remotely.
            env: Optional environment variables exported before the command runs.
            pty: Request a pseudo-terminal (default). A pty merges stderr into stdout
                and turns ``\n`` into ``\r\n``; pass ``False`` to keep the two
                streams apart, as :func:`lium.machine` does to relay a function's output.

        Yields:
            Streaming output chunks as ``{"type": "stdout"|"stderr", "data": str}``.

        Returns:
            The command's exit status (the generator's ``StopIteration.value``).
        """
        command = self._prep_command(command, env)

        with self.ssh_connection(pod) as client:
            stdin, stdout, stderr = client.exec_command(command, get_pty=pty)
            stdin.close()

            channel = stdout.channel
            while True:
                got = False
                if channel.recv_ready():
                    data = channel.recv(4096).decode("utf-8", errors="replace")
                    if data:
                        got = True
                        yield {"type": "stdout", "data": data}

                if channel.recv_stderr_ready():
                    data = channel.recv_stderr(4096).decode("utf-8", errors="replace")
                    if data:
                        got = True
                        yield {"type": "stderr", "data": data}

                if got:
                    continue
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    return channel.recv_exit_status()
                time.sleep(0.05)  # nothing pending: do not spin at 100% CPU until the command ends

    def exec_all(
        self,
        pods: List[PodInfo],
        *,
        command: str,
        env: Optional[Dict[str, str]] = None,
        max_workers: int = 10,
    ) -> List[Dict]:
        """Execute a shell command on multiple pods in parallel.

        Args:
            pods: List of pods to target.
            command: Shell command to run on each pod.
            env: Optional environment variables exported before each command.
            max_workers: Maximum number of SSH workers to spawn.

        Returns:
            List of result dictionaries mirroring :meth:`exec`, each with the pod
            id under ``"pod"``. When SSH fails for a pod its entry is
            ``{"pod": <id>, "error": <message>, "success": False}`` — same key,
            same type as the successful entries, so callers can index results
            by pod id without checking which shape they got.

        Raises:
            ValueError: an ``env`` name is not a shell identifier. Checked once,
                before any pod is contacted — a caller's mistake is not one
                failure entry per pod.
        """
        if env:
            self._env_exports(env)  # the name check; exec() builds the exports again per pod

        def exec_single(pod: PodInfo):
            try:
                result = self.exec(pod, command=command, env=env)
                result["pod"] = pod.id
                return result
            except Exception as e:
                return {"pod": pod.id, "error": str(e), "success": False}

        with ThreadPoolExecutor(max_workers=min(max_workers, len(pods))) as executor:
            return list(executor.map(exec_single, pods))

    # Statuses a pod never leaves. Seeing one while waiting means "stop waiting",
    # not "keep polling until the timeout".
    TERMINAL_POD_STATUSES = frozenset(
        {
            "FAILED",
            "STOPPED",
            "ERROR",
            "TERMINATED",
            "DELETED",
            "REMOVED",
            "CANCELLED",
            # The backend's own names for a rent that will not come up: a failed create is
            # CREATION_FAILED for three minutes before its row is deleted, a force-closed pod is
            # BROKEN, a reboot the host never came back from is REBOOT_FAILED, a delete in flight
            # is DELETING.
            "CREATION_FAILED",
            "BROKEN",
            "REBOOT_FAILED",
            "DELETING",
        }
    )
    # How long a pod that was never listed may stay out of ``ps`` before it is declared
    # missing (a wrong id, or a rent the backend dropped). A time budget, not a poll count:
    # at the 2 s schedule a count of 3 gave a pod ~4 s to appear instead of the ~20 s it had
    # at 10 s, so one listing hiccup would have failed ``lium up`` on a pod already billing.
    MISSING_GRACE_SECONDS = 20
    # DAH-3002: the backend marks a cached-template pod RUNNING at p50 22.5 s after the rent
    # (7 d to 6 Sep 2026); polled every 10 s the caller learnt it 0–10 s late. Poll every
    # 2 s while a normal start is still plausible, then fall back to the old 10 s.
    FAST_POLL_SECONDS = 2
    FAST_POLL_WINDOW_SECONDS = 90
    SLOW_POLL_SECONDS = 10

    @classmethod
    def poll_delay(cls, elapsed: float, poll_interval: Optional[int] = None) -> float:
        """Seconds to sleep between two ``wait_ready`` polls.

        A caller-given ``poll_interval`` is used as-is; ``None`` selects the adaptive
        schedule (:attr:`FAST_POLL_SECONDS` for the first :attr:`FAST_POLL_WINDOW_SECONDS`
        seconds, :attr:`SLOW_POLL_SECONDS` after that).
        """
        if poll_interval is not None:
            return poll_interval
        return cls.FAST_POLL_SECONDS if elapsed < cls.FAST_POLL_WINDOW_SECONDS else cls.SLOW_POLL_SECONDS

    def pod_events(self, pod_id: str) -> List[Dict[str, Any]]:
        """The pod's event log, oldest first — creation, reboots, failures with their error, and
        lifecycle entries saying why it left RUNNING. Answers for a pod whose row is already gone.

        Returns ``[]`` against a backend that predates the endpoint.
        """
        try:
            # One attempt: this is read on the failure path, after the wait's budget is spent, so the
            # retry backoff would only delay the PodStartError the caller is about to see.
            data = self._request("GET", f"/pods/{quote(str(pod_id), safe='')}/events", retry=False).json()
        except LiumNotFoundError:
            return []
        # dict entries only: pod_failure_cause reads them on a failure path and must not raise there
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def pod_failure_cause(self, pod_id: str) -> Optional[str]:
        """What the backend recorded as the reason the pod failed or was closed, or ``None``.

        The latest event carrying an ``error`` (a failed create or reboot: the validator's headline)
        or a lifecycle ``reason``/``detail`` wins. Never raises — this is read on a failure path.
        """
        try:
            events = self.pod_events(pod_id)
        except Exception:
            # ``_request`` raises ``requests.RequestException`` or a ``LiumError`` on a failed call, and a
            # network blip here must not turn a ``PodStartError`` into "Unexpected error".
            return None
        for event in reversed(events):
            if event.get("error"):
                return event["error"]
            if event.get("reason"):
                detail = event.get("detail")
                return f"{event['reason']}: {detail}" if detail else event["reason"]
        return None

    def _never_listed_error(self, pod_id: str, missing_polls: int, elapsed: float) -> PodStartError:
        """The error for an id that was never in the pod list once :attr:`MISSING_GRACE_SECONDS` are spent."""
        cause = self.pod_failure_cause(pod_id)
        return PodStartError(
            f"Pod {pod_id} is not in the pod list after {missing_polls} checks over {elapsed:.0f} s"
            + (f"; cause: {cause}" if cause else ""),
            pod_id=pod_id, cause=cause,
        )

    def wait_ready(
        self,
        pod: Union[str, PodInfo, Dict],
        *,
        timeout: Optional[int] = 300,
        poll_interval: Optional[int] = None,
        on_poll: Optional[Callable[[Optional[PodInfo], str, float], None]] = None,
    ) -> Optional[PodInfo]:
        """Poll until a pod reports RUNNING + SSH metadata.

        Args:
            pod: Pod identifier, PodInfo, or dict with an ``id`` field.
            timeout: Maximum number of seconds to wait; ``None`` waits until the
                pod is ready or fails.
            poll_interval: Fixed interval between successive ``ps`` calls; ``None``
                (default) polls every :attr:`FAST_POLL_SECONDS` for the first
                :attr:`FAST_POLL_WINDOW_SECONDS` seconds, then every
                :attr:`SLOW_POLL_SECONDS` — see :meth:`poll_delay`.
            on_poll: Called after every poll with the pod as last listed (or
                ``None``), its status (``"missing"`` when not listed) and the
                seconds elapsed, so a caller can show progress while waiting.

        Returns:
            PodInfo when the pod is ready, otherwise ``None`` if the timeout
            expires while the pod is still starting.

        Raises:
            PodStartError: The pod reached a terminal status (``FAILED``,
                ``CREATION_FAILED``, ``STOPPED``, …), vanished from the pod list
                after being seen, or was still not listed
                :attr:`MISSING_GRACE_SECONDS` seconds after the first poll. The error carries the
                last ``PodInfo``, its status, the status history and the cause
                the backend recorded (``cause``), so a caller can tell a dead pod
                from a slow one and clean up instead of retrying.
        """
        if isinstance(pod, PodInfo):
            pod_id = pod.id
        elif isinstance(pod, dict) and 'id' in pod:
            pod_id = pod['id']
        else:
            pod_id = pod

        start = time.time()
        history: List[str] = []
        last_seen: Optional[PodInfo] = None
        missing_polls = 0
        while True:
            elapsed = time.time() - start
            if timeout is not None and elapsed >= timeout:
                if last_seen is None and missing_polls and elapsed >= self.MISSING_GRACE_SECONDS:
                    # The budget and the grace ran out together: an id that was never listed in
                    # 20 s is a missing pod, not a slow one — say so instead of answering None
                    # (wait_ready('00000000-…', timeout=20), the DAH-1942 audit case).
                    raise self._never_listed_error(pod_id, missing_polls, elapsed)
                break
            fresh_pods = self.ps()
            current = next((p for p in fresh_pods if p.id == pod_id), None)

            if current is None:
                missing_polls += 1
                if on_poll:
                    on_poll(last_seen, "missing", elapsed)
                if last_seen is not None:
                    cause = self.pod_failure_cause(pod_id)
                    raise PodStartError(
                        f"Pod {last_seen.huid} ({pod_id}) disappeared while starting; "
                        f"last status {history[-1] if history else 'unknown'}"
                        + (f"; cause: {cause}" if cause else ""),
                        pod_id=pod_id, pod=last_seen, status=history[-1] if history else None,
                        history=history, cause=cause,
                    )
                if elapsed >= self.MISSING_GRACE_SECONDS:
                    raise self._never_listed_error(pod_id, missing_polls, elapsed)
                time.sleep(self.poll_delay(elapsed, poll_interval))
                continue

            missing_polls = 0
            last_seen = current
            status = (current.status or "unknown").upper()
            if not history or history[-1] != status:
                history.append(status)
            if on_poll:
                on_poll(current, status, elapsed)

            if status == "RUNNING" and current.ssh_cmd:
                return current
            if status in self.TERMINAL_POD_STATUSES:
                cause = self.pod_failure_cause(pod_id)
                raise PodStartError(
                    f"Pod {current.huid} ({pod_id}) will not start: status {status}"
                    f" (seen: {' → '.join(history)})" + (f"; cause: {cause}" if cause else ""),
                    pod_id=pod_id, pod=current, status=status, history=history, cause=cause,
                )

            time.sleep(self.poll_delay(elapsed, poll_interval))
        return None

    def scp(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a local file to a pod via SFTP."""
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.put(local, remote)
            sftp.close()

    def download(self, pod: PodInfo, *, remote: str, local: str) -> None:
        """Download a file from a pod via SFTP.

        Args:
            pod: The pod to download from.
            remote: Remote file path on the pod.
            local: Local destination path.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        with self.ssh_connection(pod) as client:
            sftp = client.open_sftp()
            sftp.get(remote, local)
            sftp.close()

    def upload(self, pod: PodInfo, *, local: str, remote: str) -> None:
        """Upload a file to a pod via SFTP.

        This is an alias for :meth:`scp` for parity with the CLI.

        Args:
            pod: The pod to upload to.
            local: Local file path to upload.
            remote: Remote destination path on the pod.

        Raises:
            ValueError: If SSH is not configured for the pod.
        """
        self.scp(pod, local=local, remote=remote)

    def ssh_argv(self, pod: PodInfo) -> List[str]:
        """The OpenSSH argument list that opens a shell on a pod.

        Built from the user, host and port the API's ``ssh_connect_cmd`` names
        (:func:`ssh_target`; any other shape is refused), with ``-i <configured
        key>`` when one is configured and the host-key options of
        :func:`openssh_host_key_options`. Run it with ``subprocess.run(argv)`` —
        no shell is involved, so nothing in the API's value is ever interpreted.

        Raises:
            ValueError: The pod has no SSH command, or it is not ``ssh <user>@<host> [-p <port>]``.
        """
        user, host, port = ssh_target(pod.ssh_cmd)
        argv = ["ssh"]
        if self.config.ssh_key_path:
            argv += ["-i", str(Path(self.config.ssh_key_path).expanduser())]
        argv += ["-p", str(port), *openssh_host_key_options(pod), f"{user}@{host}"]
        return argv

    def refresh_pod(self, pod: Union[str, PodInfo]) -> PodInfo:
        """Re-read one pod from the API.

        A ``PodInfo`` is a snapshot: after a restart the pod's host, port and
        ``ssh_cmd`` can all change, and the copy a caller holds says nothing
        about it. Call this before reconnecting to a pod held for a while.

        Args:
            pod: A ``PodInfo``, a pod id or a huid.

        Returns:
            The current ``PodInfo`` for that pod (one ``/pods`` call).

        Raises:
            LiumNotFoundError: The pod is no longer in the account's pod list.
        """
        pod_id = pod.id if isinstance(pod, PodInfo) else pod
        current = next((p for p in self.ps() if pod_id in (p.id, p.huid)), None)
        if current is None:
            raise LiumNotFoundError(f"Pod not found: {pod_id}")
        return current

    def ssh(self, pod: PodInfo, *, refresh: bool = False) -> str:
        """Get SSH command string for connecting to a pod.

        The shell-quoted form of :meth:`ssh_argv`: the configured key, the pinned
        host-key options and the pod's ``user@host``, ready to paste into a POSIX shell.
        Building it creates the pod's ``~/.lium/known_hosts/<pod id>`` file when it does
        not exist yet (empty until the first connection pins the key).

        Args:
            pod: The pod to generate SSH command for.
            refresh: Re-read the pod first (see :meth:`refresh_pod`), so the
                command reflects the host and port the pod has *now*, not the
                ones it had when ``pod`` was fetched.

        Returns:
            SSH command string with the configured SSH key path.

        Raises:
            ValueError: If SSH is not configured for the pod or no SSH key path is set.
            LiumNotFoundError: ``refresh=True`` and the pod is gone.
        """
        if refresh:
            pod = self.refresh_pod(pod)
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        return shlex.join(self.ssh_argv(pod))

    @staticmethod
    def rsync_options(
        *,
        bwlimit: Optional[int] = None,
        exclude: Optional[Sequence[str]] = None,
        delete: bool = False,
        partial: bool = True,
        progress: bool = False,
    ) -> List[str]:
        """The rsync flags shared by :meth:`rsync` and :meth:`cp`.

        ``partial`` keeps half-copied files so an interrupted transfer resumes
        instead of starting over — multi-GB pulls over a flaky link are the
        normal case, not the exception. It stays ``--partial`` only: ``--inplace``
        would drop rsync's write-then-rename, and a job on the pod could read a
        half-written checkpoint. ``progress`` is plain ``--progress``, which both
        GNU rsync and the openrsync stock macOS ships accept (``--info=progress2``
        needs GNU rsync >= 3.1 and dies before any byte moves on a Mac).
        """
        options = ["-az"]
        if partial:
            options += ["--partial"]
        if progress:
            options += ["--progress"]
        if bwlimit is not None:
            if bwlimit <= 0:
                raise ValueError("bwlimit must be a positive number of KiB/s")
            options += [f"--bwlimit={int(bwlimit)}"]
        for pattern in exclude or ():
            options += [f"--exclude={pattern}"]
        if delete:
            options += ["--delete"]
        return options

    def rsync(
        self,
        pod: PodInfo,
        *,
        local: str,
        remote: str,
        bwlimit: Optional[int] = None,
        exclude: Optional[Sequence[str]] = None,
        delete: bool = False,
        partial: bool = True,
        progress: bool = False,
        download: bool = False,
    ) -> None:
        """Sync files between the local machine and a pod with rsync.

        Args:
            pod: Pod to sync.
            local: Local path (source, or destination when ``download``).
            remote: Path on the pod (destination, or source when ``download``).
            bwlimit: Cap the transfer at this many KiB/s (rsync ``--bwlimit``).
            exclude: Patterns to skip (rsync ``--exclude``), e.g. ``[".git", "*.pt"]``.
            delete: Remove files at the destination that are not in the source.
            partial: Keep partially transferred files so a retry resumes (default on).
            progress: Show rsync's overall progress on the terminal instead of
                capturing its output.
            download: Copy from the pod to the local path instead of to it.

        Raises:
            RuntimeError: If the rsync command fails.
        """
        if not pod.ssh_cmd or not self.config.ssh_key_path:
            raise ValueError("No SSH configured")

        user, host, port = ssh_target(pod.ssh_cmd)
        ssh_cmd = shlex.join(
            ["ssh", "-i", str(self.config.ssh_key_path), "-p", str(port), *openssh_host_key_options(pod)]
        )
        remote_spec = f"{user}@{host}:{remote}"
        endpoints = [remote_spec, local] if download else [local, remote_spec]
        cmd = [
            "rsync",
            *self.rsync_options(bwlimit=bwlimit, exclude=exclude, delete=delete, partial=partial, progress=progress),
            "-e", ssh_cmd,
            *endpoints,
        ]

        result = subprocess.run(cmd, capture_output=not progress, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Rsync failed: {(result.stderr or '').strip() or f'exit {result.returncode}'}")

    def cp(
        self,
        src_pod: PodInfo,
        src_path: str,
        dst_pod: PodInfo,
        dst_path: str,
        *,
        bwlimit: Optional[int] = None,
        exclude: Optional[Sequence[str]] = None,
        delete: bool = False,
    ) -> Dict[str, Any]:
        """Copy files from one pod to another over SSH, without passing through this machine.

        Pod-to-pod links are far faster than relaying through the caller. The
        source pod gets a one-off ed25519 key, its public half is added to the
        destination pod's ``authorized_keys`` for the duration of the copy, the
        source runs ``rsync`` straight to the destination, and both halves are
        removed again whatever happened. The source verifies the destination with
        the host key this client pinned for it (``~/.lium/known_hosts/<pod id>``,
        written by the grant connection), copied next to the transfer key; no pin
        means no copy (``LIUM_SSH_INSECURE=1`` accepts any key, as everywhere).

        Args:
            src_pod, src_path: Where to copy from. A trailing ``/`` on a
                directory copies its contents, as in rsync.
            dst_pod, dst_path: Where to copy to.
            bwlimit, exclude, delete: As in :meth:`rsync`.

        Returns:
            The :meth:`exec` result of the rsync on the source pod.

        Raises:
            LiumHostKeyError: When nothing is pinned for the destination pod.
            LiumError: When the copy fails; the message carries rsync's stderr
                (``rsync: command not found`` means ``apt-get install -y rsync``
                on the pod named).
        """
        if src_pod.id == dst_pod.id:
            result = self.exec(
                src_pod,
                command=f"rsync {shlex.join(self.rsync_options(bwlimit=bwlimit, exclude=exclude, delete=delete))} "
                        f"{shlex.quote(src_path)} {shlex.quote(dst_path)}",
            )
            if not result["success"]:
                raise LiumError(f"Copy on pod {src_pod.name or src_pod.huid} failed: {result['stderr'].strip()}")
            return result

        if not dst_pod.ssh_cmd or not dst_pod.host:
            raise ValueError(f"No SSH for destination pod {dst_pod.name or dst_pod.huid}")

        key_path = f"/tmp/lium-cp-{uuid.uuid4().hex[:12]}"
        keygen = self.exec(
            src_pod,
            command=f"ssh-keygen -q -t ed25519 -N '' -f {key_path} && cat {key_path}.pub",
        )
        if not keygen["success"]:
            raise LiumError(
                f"Could not create a transfer key on pod {src_pod.name or src_pod.huid}: "
                f"{keygen['stderr'].strip() or keygen['stdout'].strip()}"
            )
        public_key = keygen["stdout"].strip().splitlines()[-1]
        marker = f"lium-cp-{uuid.uuid4().hex[:12]}"
        authorized_line = f"{public_key} {marker}"

        authorized = False
        try:
            grant = self.exec(dst_pod, command=self.grant_transfer_key_command(authorized_line))
            if not grant["success"]:
                # `flock -w 30` gives up silently (exit 1) when another cp holds the lock
                detail = grant["stderr"].strip() or f"exit {grant.get('exit_code')} (another copy may hold {self.TRANSFER_KEY_LOCK})"
                raise LiumError(f"Could not authorise the transfer key on pod {dst_pod.name or dst_pod.huid}: {detail}")
            authorized = True

            # The source pod must verify the destination the way this client does: the grant above went
            # through ssh_connection, which pinned dst's host key under ~/.lium/known_hosts/<pod id>, so
            # that pin is copied next to the transfer key and ssh on the source is told to insist on it.
            # LIUM_SSH_INSECURE=1 keeps the old accept-anything hop, like every other SSH path here.
            if ssh_insecure():
                host_key_opts = "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null"
            else:
                pinned = self._pinned_host_key_lines(dst_pod)
                if not pinned:
                    raise LiumHostKeyError(
                        f"No pinned host key for pod {dst_pod.name or dst_pod.huid} under "
                        f"{known_hosts_path(dst_pod)}; the transfer key was not used. Connect to it once "
                        f"(lium ssh {dst_pod.huid}) or set {_SSH_INSECURE_ENV}=1 to skip host key checks."
                    )
                pin_copy = self.exec(
                    src_pod,
                    command=f"printf '%s\\n' {shlex.quote(pinned)} > {key_path}.known_hosts && chmod 600 {key_path}.known_hosts",
                )
                if not pin_copy["success"]:
                    raise LiumError(
                        f"Could not place the destination's host key on pod {src_pod.name or src_pod.huid}: "
                        f"{pin_copy['stderr'].strip()}"
                    )
                host_key_opts = f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={key_path}.known_hosts"
            ssh_opts = f"ssh -i {key_path} -p {dst_pod.ssh_port} {host_key_opts} -o LogLevel=ERROR"
            rsync_cmd = (
                f"rsync {shlex.join(self.rsync_options(bwlimit=bwlimit, exclude=exclude, delete=delete))} "
                f"-e {shlex.quote(ssh_opts)} {shlex.quote(src_path)} "
                f"{shlex.quote(f'{dst_pod.username}@{dst_pod.host}:{dst_path}')}"
            )
            result = self.exec(src_pod, command=rsync_cmd)
            if not result["success"]:
                detail = result["stderr"].strip() or result["stdout"].strip() or f"exit {result['exit_code']}"
                raise LiumError(
                    f"Copy from {src_pod.name or src_pod.huid}:{src_path} to "
                    f"{dst_pod.name or dst_pod.huid}:{dst_path} failed: {detail}"
                )
            return result
        finally:
            if authorized:
                revoke = self.revoke_transfer_key_command(marker)
                self._exec_quietly(
                    dst_pod,
                    revoke,
                    consequence=(
                        f"the transfer key '{marker}' is still authorised on pod "
                        f"{dst_pod.name or dst_pod.huid}; revoke it with: "
                        f"lium exec {dst_pod.huid} {shlex.quote(revoke)}"
                    ),
                )
            self._exec_quietly(src_pod, f"rm -f {key_path} {key_path}.pub {key_path}.known_hosts")

    @staticmethod
    def _pinned_host_key_lines(pod: PodInfo) -> str:
        """The destination's pinned host key(s) as ``known_hosts`` lines, ``""`` when nothing is pinned.

        ``ssh_connection`` writes the file (``[host]:port key-type key``) on the first connection to the pod;
        the file is per pod, so every line in it is this pod's.
        """
        try:
            text = known_hosts_path(pod).read_text()
        except OSError:
            return ""
        return "\n".join(line for line in text.splitlines() if line.strip() and not line.startswith("#"))

    # Every grant and revoke on a pod runs under this lock: two concurrent ``cp``
    # into the same pod otherwise both filter the same authorized_keys and the
    # later ``cat >`` puts back the key the earlier revoke removed.
    TRANSFER_KEY_LOCK = "~/.ssh/.lium-cp.lock"

    @classmethod
    def _under_transfer_key_lock(cls, command: str) -> str:
        """``command`` run by ``flock`` on the pod's transfer-key lock (30 s wait, then fail)."""
        return f"flock -w 30 {cls.TRANSFER_KEY_LOCK} -c {shlex.quote(command)}"

    @classmethod
    def grant_transfer_key_command(cls, authorized_line: str) -> str:
        """The remote line that appends ``authorized_line`` to the pod's authorized_keys."""
        return (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            + cls._under_transfer_key_lock(
                f"echo {shlex.quote(authorized_line)} >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
            )
        )

    @classmethod
    def revoke_transfer_key_command(cls, marker: str) -> str:
        """The remote line that drops the authorized_keys entry tagged ``marker``.

        The scratch file carries the marker, so two ``cp`` runs into the same
        pod never share one, and the result is written back with ``cat >``
        (the way lium-io removes keys) rather than ``mv``: the file keeps its
        mode and a second run's half-written scratch file can never replace it.
        ``grep`` exits 1 when nothing is left to keep, which is fine; any other
        failure leaves authorized_keys untouched. The whole line runs under the
        transfer-key lock, so a concurrent grant or revoke waits for it.
        """
        scratch = f"~/.ssh/authorized_keys.{marker}"
        return cls._under_transfer_key_lock(
            f"( grep -vF {shlex.quote(marker)} ~/.ssh/authorized_keys > {scratch} || [ $? -eq 1 ] ) "
            f"&& cat {scratch} > ~/.ssh/authorized_keys; rc=$?; rm -f {scratch}; exit $rc"
        )

    def _exec_quietly(self, pod: PodInfo, command: str, *, consequence: Optional[str] = None) -> None:
        """Cleanup step: report a failure as a warning, never as the error the caller sees.

        ``consequence`` says what a failure leaves behind and how to undo it by hand.
        """
        where = f"pod {pod.name or pod.huid}"
        try:
            result = self.exec(pod, command=command)
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise
            detail = str(exc)
        else:
            if result["success"]:
                return
            detail = result["stderr"].strip() or f"exit {result['exit_code']}"
        message = f"lium: cleanup on {where} failed ({detail})"
        message += f": {consequence}" if consequence else f": {command}"
        warnings.warn(message, stacklevel=3)
    
    def switch_template(self, pod: PodInfo, *, template_id: str) -> PodInfo:
        """Switch the template of a running pod.
        
        Args:
            pod: Pod to update.
            template_id: ID of the template to switch to.
            
        Returns:
            PodInfo object with updated pod information.
        """
        payload = {
            "template_id": template_id
        }
        
        response = self._request("PUT", f"/pods/{pod.id}/switch-template", json=payload).json()
        forget_host_key(pod)  # new container, new host key
        
        # Parse the response into a PodInfo object
        return PodInfo(
            id=pod.id,  # Keep the original pod ID
            name=response.get("pod_name", pod.name),
            status=response.get("status", "PENDING"),
            huid=pod.huid,  # Keep the original HUID
            ssh_cmd=response.get("ssh_connect_cmd"),
            ports=response.get("ports_mapping", {}),
            created_at=response.get("created_at", ""),
            updated_at=response.get("updated_at", ""),
            executor=ExecutorInfo(
                id=response.get("executor_id", ""),
                huid="",
                machine_name="",
                gpu_type=response.get("gpu_name", ""),
                gpu_count=int(response.get("gpu_count", 0) or 0),
                price_per_hour=0.0,
                price_per_gpu=0.0,
                location={},
                specs={},
                status="",
                docker_in_docker=False
            ) if response.get("executor_id") else None,
            template={"id": response.get("template_id", template_id)},
            removal_scheduled_at=None,
            jupyter_installation_status=None,
            jupyter_url=None,
            enable_volume_encryption=response.get("enable_volume_encryption"),
            volume_encryption_status=response.get("volume_encryption_status"),
        )

    
    def create_template(
        self,
        name: str,
        docker_image: str,
        docker_image_digest: str = "",
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Create a new template.

        Args:
            name: Friendly template name.
            docker_image: Image repository (e.g., ``"daturaai/pytorch"``).
            docker_image_digest: Digest string for pinning (defaults to empty string).
            docker_image_tag: Image tag (defaults to ``"latest"``).
            ports: Internal ports to expose (defaults to ``[22, 8000]``).
            start_command: Optional command executed on container start.
            **kwargs: Additional template fields:
                - category (str): Template category (defaults to ``"UBUNTU"``).
                - is_private (bool): Whether template is private (defaults to ``True``).
                - volumes (List[str]): Volume mount paths (defaults to ``["/workspace"]``).
                - description (str): Template description.
                - environment (Dict[str, str]): Environment variables.
                - entrypoint (str): Container entrypoint.
                - one_time_template (bool): Whether to delete template after pod removal
                  (defaults to ``False``).

        Returns:
            Newly created :class:`Template`.
        """
        payload = {
            "name": name,
            "docker_image": docker_image,
            "docker_image_digest": docker_image_digest,
            "docker_image_tag": docker_image_tag,
            "internal_ports": ports or [22, 8000],
            "startup_commands": start_command or "",
            "category": kwargs.get("category", "UBUNTU"),
            "container_start_immediately": kwargs.get("container_start_immediately", True),
            "description": kwargs.get("description", name),
            "entrypoint": kwargs.get("entrypoint", ""),
            "environment": kwargs.get("environment") or {},
            "is_private": kwargs.get("is_private", True),
            "one_time_template": kwargs.get("one_time_template", False),
            # Internal backend clone marker; intentionally omitted from the public SDK docs.
            "is_temporary": kwargs.get("is_temporary", False),
            "readme": kwargs.get("readme", name),
            "volumes": kwargs.get("volumes", ["/workspace"]),
        }

        response = self._request("POST", "/templates", json=payload).json()
        return Template(
            id=response.get("id", ""),
            huid=generate_huid(response.get("id", "")),
            name=response.get("name", ""),
            docker_image=response.get("docker_image", ""),
            docker_image_tag=response.get("docker_image_tag", "latest"),
            category=response.get("category", "general"),
            status=response.get("status", "unknown"),
        )

    def wait_template_ready(self, template_id: str, timeout: int = 300) -> Optional[Template]:
        """Wait for template verification to complete.

        Args:
            template_id: Template identifier.
            timeout: Maximum seconds to wait.

        Returns:
            Template when verification succeeds, otherwise ``None`` if the timeout expires.

        Raises:
            LiumError: If template verification fails.
        """

        start = time.time()
        while time.time() - start < timeout:
            templates = self.templates(only_my=True)
            current = next((t for t in templates if t.id == template_id), None)

            if current:
                status = current.status.upper()
                if status == "VERIFY_SUCCESS":
                    return current
                elif status == "VERIFY_FAILED":
                    raise LiumError(f"Template verification failed: {current.name}")

            time.sleep(10)
        return None

    def me(self) -> Dict[str, Any]:
        """The account the API key belongs to, as ``GET /users/me`` returns it.

        Useful keys: ``id``, ``email`` (when the server sends it), ``balance``.
        """
        data = self._request("GET", "/users/me").json()
        return data if isinstance(data, dict) else {}

    def get_my_user_id(self) -> str:
        """Get the current user's ID.

        Returns:
            The ID returned by ``/users/me``.
        """
        return self.me()["id"]

    def update_template(
        self,
        template_id: str,
        name: str,
        docker_image: str,
        docker_image_digest: str,
        docker_image_tag: str = "latest",
        ports: Optional[List[int]] = None,
        start_command: Optional[str] = None,
        **kwargs
    ) -> Template:
        """Update an existing template owned by the caller.

        Args:
            template_id: Template identifier.
            name: Friendly name.
            docker_image: Image repository.
            docker_image_digest: Optional digest.
            docker_image_tag: Image tag.
            ports: Internal ports to expose.
            start_command: Startup command.
            **kwargs: Additional override fields.

        Returns:
            Updated :class:`Template`.

        Raises:
            ValueError: If the template is missing or not owned by the caller.
        """
        templates = self._request("GET", "/templates").json()
        current = next((t for t in templates if t["id"] == template_id), None)

        if not current:
            raise ValueError(f"Template with ID {template_id} not found")

        if current.get("user_id") != self.get_my_user_id():
            raise ValueError(f"Cannot update template {template_id}: not owned by current user")

        payload = current.copy()
        payload.update({
                "name": name,
                "docker_image": docker_image,
                "docker_image_digest": docker_image_digest,
                "docker_image_tag": docker_image_tag,
                "internal_ports": ports or [22, 8000],
                "startup_commands": start_command or "",
                "category": kwargs.get("category", payload.get("category", "UBUNTU")),
                "container_start_immediately": kwargs.get("container_start_immediately", payload.get("container_start_immediately", True)),
                "description": kwargs.get("description", payload.get("description", name)),
                "entrypoint": kwargs.get("entrypoint", payload.get("entrypoint", "")),
                "environment": kwargs.get("environment", payload.get("environment", {})),
                "is_private": kwargs.get("is_private", payload.get("is_private", False)),
                "readme": kwargs.get("readme", payload.get("readme", name)),
                "volumes": kwargs.get("volumes", payload.get("volumes", [])),
        })

        resp = self._request("PUT", f"/templates/{template_id}", json=payload).json()
        return Template(
            id=template_id,
            huid=generate_huid(template_id),
            name=payload['name'],
            docker_image=payload['docker_image'],
            docker_image_tag=payload['docker_image_tag'],
            category=payload['category'],
            status=resp.get("status", "unknown"),
        )


    def wallets(self) -> List[Dict[str, Any]]:
        """Get the caller's configured funding wallets.

        Returns:
            Raw wallet records returned by the pay API.
        """
        user = self._request("GET", "/users/me").json()
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        resp = self._request(
            "GET",
            f"/wallet/available-wallets/{user['stripe_customer_id']}",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
        )
        return resp.json()

    def _create_transfer_app_credentials(self) -> tuple[str, str]:
        """Read ``(app_id, customer_id)`` from the ``/tao/create-transfer`` redirect.

        This is the single parse both :meth:`add_wallet` and :meth:`_discover_app_id`
        share, so the alpha flow never issues more than one ``/tao/create-transfer``
        round-trip.

        Note the deliberate HTTP shape: this call uses the DEFAULT ``base_url`` (the
        main Lium API) and NO pay ``X-API-KEY`` header — unlike the pay-API calls
        (:meth:`wallets`, :meth:`convert_alpha`, :meth:`company_wallet`). Do not
        "harmonize" it onto ``base_pay_url`` / the pay key; that returns 401/404.
        """
        create_transfer_response = self._request(
            "POST", "/tao/create-transfer", json={"amount": 10}
        )
        redirect_url = create_transfer_response.json()["url"]
        params = parse_qs(urlparse(redirect_url).query)
        return params["app_id"][0], params["customer_id"][0]

    def _discover_app_id(self, bt_wallet: Any = None) -> str:
        """Resolve the pay-app id without registering a wallet.

        Reuses :meth:`_create_transfer_app_credentials`, so it works identically
        whether or not the coldkey is already registered. ``bt_wallet`` is accepted
        for call-site symmetry but unused (the create-transfer parse needs no wallet).
        """
        app_id, _ = self._create_transfer_app_credentials()
        return app_id

    def add_wallet(self, bt_wallet: Any) -> tuple[str, str]:
        """Link a Bittensor wallet with the user account.

        Args:
            bt_wallet: Wallet object exposing ``coldkey``/``coldkeypub`` for signing.

        Returns:
            ``(app_id, customer_id)`` parsed from the ``/tao/create-transfer``
            redirect — surfaced so the alpha funding flow can reuse the same single
            round-trip for company-wallet lookup instead of issuing a second POST.

        Raises:
            LiumError: If verification or wallet polling fails.
        """
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        access_key = self._request(
            "GET", "/token/generate", base_url=self.config.base_pay_url, headers=pay_headers
        ).json()["access_key"]
        sig = bt_wallet.coldkey.sign(access_key.encode()).hex()
        app_id, stripe_customer_id = self._create_transfer_app_credentials()

        verify_response = self._request(
            "POST",
            "/token/verify",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
            json={
                "coldkey_address": bt_wallet.coldkeypub.ss58_address,
                "access_key": access_key,
                "signature": sig,
                "stripe_customer_id": stripe_customer_id,
                "application_id": app_id,
            },
        )
        if verify_response.json()["status"].lower() != "ok":
            raise LiumError(f"Failed to add wallet: {verify_response.text}")

        for i in range(5):
            wallets = [w.get('wallet_hash', '') for w in self.wallets()]
            if bt_wallet.coldkeypub.ss58_address in wallets:
                return app_id, stripe_customer_id
            time.sleep(2)
        raise LiumError("Failed to add wallet. Wallet not found after 5 attempts.")

    def convert_alpha(self, usd: Any) -> AlphaQuote:
        """Quote ``usd`` (USD) -> alpha via ``GET /balance/convert/alpha``.

        The response carries both the alpha amount to transfer (``converted``) and
        the subnet ``netuid`` the transfer must happen on. Hard-fails (no fallback)
        on a pay-API error: ``_request`` maps 503 -> ``LiumServerError`` (a
        ``LiumError``), so a down subtensor / unavailable alpha price aborts the
        fund before any on-chain call.
        """
        pay_headers = {"X-API-KEY": _PAY_API_KEY}
        resp = self._request(
            "GET",
            "/balance/convert/alpha",
            base_url=self.config.base_pay_url,
            headers=pay_headers,
            params={"amount": str(usd)},
        ).json()
        return AlphaQuote(
            usd=Decimal(str(resp["original"])),
            alpha_amount=Decimal(str(resp["converted"])),
            rate=Decimal(str(resp["rate"])),
            netuid=int(resp["netuid"]),
        )

    def company_wallet(self, app_id: str) -> str:
        """Resolve the Lium destination coldkey via ``GET /wallet/company/?app_id=``.

        Returns the company ``wallet_hash`` (the SS58 the pay-tao-api-v2 listener
        credits). Hard-fails (no fallback): a 404 (app has no wallet) maps to
        ``LiumNotFoundError`` (a ``LiumError``), aborting before any on-chain call.
        """
        resp = self._request(
            "GET",
            "/wallet/company/",
            base_url=self.config.base_pay_url,
            headers={"X-API-KEY": _PAY_API_KEY},
            params={"app_id": app_id},
        ).json()
        return resp["wallet_hash"]

    def backup_create(
        self,
        pod: PodInfo,
        *,
        path: str,
        frequency_hours: int = 6,
        retention_days: int = 7,
    ) -> BackupConfig:
        """Create or replace a backup configuration for a pod.

        Args:
            pod: Pod to configure.
            path: Explicit filesystem path inside the pod volume to back up.
            frequency_hours: Backup interval in hours.
            retention_days: Retention period in days.

        Returns:
            Created :class:`BackupConfig`.
        """
        if self.source != "cli" and path.rstrip("/") == pod.volume_path.rstrip("/"):
            warnings.warn(
                "Backing up the entire volume is less reliable when files are actively changing; "
                "prefer a stable subdirectory when possible.",
                UserWarning,
                stacklevel=2,
            )
        payload = {
            "pod_id": pod.id,
            "backup_frequency_hours": frequency_hours,
            "retention_days": retention_days,
            "backup_path": path
        }
        
        response = self._request("POST", "/backup-configs", json=payload).json()
        
        return self._dict_to_backup_config(response)

    def backup_now(
        self,
        pod: PodInfo,
        *,
        name: str,
        description: str = "",
    ) -> Dict[str, Any]:
        """Trigger an immediate backup for a pod.

        Args:
            pod: Pod to back up.
            name: Backup name.
            description: Optional description.

        Returns:
            API response payload from the run-now endpoint.
        """
        payload = {
            "name": name,
            "description": description
        }
        
        return self._request("POST", f"/pods/{pod.id}/backup", json=payload).json()

    def backup_config(self, pod: PodInfo) -> Optional[BackupConfig]:
        """Return the backup configuration for a pod if one exists.

        Args:
            pod: Pod to inspect.

        Returns:
            :class:`BackupConfig` if present, otherwise ``None``.
        """
        try:
            response = self._request("GET", f"/backup-configs/pod/{pod.id}").json()
            return self._dict_to_backup_config(response) if response else None
        except LiumNotFoundError:
            # No backup config exists for this pod
            return None
    
    def backup_list(self) -> List[BackupConfig]:
        """List all backup configurations across all pods.

        Returns:
            List of :class:`BackupConfig`.
        """
        configs = self._request("GET", "/backup-configs").json()
        return [self._dict_to_backup_config(c) for c in configs]

    def backup_logs(self, pod: PodInfo) -> List[BackupLog]:
        """Get recent backup logs for a pod.

        Args:
            pod: Pod to inspect.

        Returns:
            List of :class:`BackupLog` entries (possibly empty).
        """
        try:
            response = self._request("GET", f"/backup-logs/pod/{pod.id}").json()
            
            # Handle paginated response - extract items from the response
            if isinstance(response, dict) and 'items' in response:
                logs = response['items']
            else:
                # Fallback for non-paginated response
                logs = response if isinstance(response, list) else []
            
            return [self._dict_to_backup_log(log) for log in logs]
        except LiumNotFoundError:
            # No backup logs exist for this pod, return empty list
            return []

    def backup_logs_all(self) -> List[BackupLog]:
        """Get all backup logs available to the current user."""
        logs: List[BackupLog] = []
        page = 1
        while True:
            response = self._request(
                "GET", "/backup-logs/", params={"page": page, "limit": 100}
            ).json()
            if not isinstance(response, dict):
                return logs
            logs.extend(self._dict_to_backup_log(log) for log in response.get("items", []))
            if not response.get("has_next"):
                return logs
            page += 1

    def backup_log(self, backup_id: str) -> BackupLog:
        """Get one backup log owned by the authenticated user."""
        response = self._request("GET", f"/backup-logs/{backup_id}").json()
        return self._dict_to_backup_log(response)

    def resolve_backup_id(self, backup_id: str) -> str:
        """Resolve an eight-character backup ID shown by the CLI."""
        if not re.fullmatch(r"[0-9a-fA-F]{8}", backup_id):
            return backup_id
        normalized_backup_id = backup_id.lower()
        matches = {
            log.id
            for log in self.backup_logs_all()
            if log.id.startswith(normalized_backup_id)
        }
        return self._resolve_short_id(backup_id, matches, "backup")

    def backup_delete(self, config_id: str) -> Dict[str, Any]:
        """Delete a backup configuration by ID.

        Args:
            config_id: Backup configuration identifier.

        Returns:
            API response payload.
        """
        return self._request("DELETE", f"/backup-configs/{config_id}").json()

    def backup_cancel(self, backup_id: str) -> Dict[str, Any]:
        """Request cancellation of an active backup while retaining its history."""
        # Idempotent payload: a repeat after the first cancel took is answered with an error, never a
        # second action, so a 5xx or a lost response is retried.
        return self._request("POST", f"/backup-logs/{quote(str(backup_id), safe='')}/cancel", retry=True).json()

    def backup_log_delete(self, backup_id: str) -> Dict[str, Any]:
        """Delete the stored data for a completed backup and retain its audit row."""
        return self._request("DELETE", f"/backup-logs/{backup_id}").json()
    
    def restore(
        self,
        pod: PodInfo,
        *,
        backup_id: str,
        restore_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Restore a backup to a pod.
        
        Args:
            pod: Pod to restore to.
            backup_id: ID of the backup to restore.
            restore_path: New or empty subdirectory where the backup is restored.
                Defaults to ``<pod volume>/restored``.
            
        Returns:
            Response from the restore API.
        """
        target_path = restore_path or pod.default_restore_path
        payload = {
            "backup_id": backup_id,
            "restore_path": target_path,
        }
        
        return self._request("POST", f"/pods/{pod.id}/restore", json=payload).json()

    def restore_logs(self, pod: PodInfo) -> List[RestoreLog]:
        """Get recent restore logs for a pod.

        Args:
            pod: Pod to inspect.

        Returns:
            List of :class:`RestoreLog` entries (possibly empty).
        """
        try:
            response = self._request("GET", f"/pods/{pod.id}/restore-logs").json()

            if isinstance(response, dict) and "items" in response:
                logs = response["items"]
            else:
                logs = response if isinstance(response, list) else []

            return [self._dict_to_restore_log(log) for log in logs]
        except LiumNotFoundError:
            return []

    def resolve_restore_id(self, restore_id: str) -> str:
        """Resolve an eight-character restore ID shown by the CLI."""
        if not re.fullmatch(r"[0-9a-fA-F]{8}", restore_id):
            return restore_id
        normalized_restore_id = restore_id.lower()
        matches = {
            log.id
            for pod in self.ps()
            for log in self.restore_logs(pod)
            if log.id.startswith(normalized_restore_id)
        }
        return self._resolve_short_id(restore_id, matches, "restore")

    @staticmethod
    def _resolve_short_id(short_id: str, matches: set[str], resource_name: str) -> str:
        if not matches:
            raise LiumNotFoundError(f"No {resource_name} matches ID '{short_id}'")
        if len(matches) > 1:
            raise LiumError(f"{resource_name.capitalize()} ID '{short_id}' is ambiguous")
        return matches.pop()

    def restore_cancel(self, restore_id: str) -> Dict[str, Any]:
        """Request cancellation of an active restore."""
        # Idempotent payload: a repeat after the first cancel took is answered with an error, never a
        # second action, so a 5xx or a lost response is retried.
        return self._request("POST", f"/restore-logs/{quote(str(restore_id), safe='')}/cancel", retry=True).json()

    def get_deployment_estimate(self, executor_id: str, template_id: str) -> dict:
        """Estimate deployment time for a template on a node.

        Args:
            executor_id: Node UUID.
            template_id: Template UUID.

        Returns:
            Dict with ``estimated_seconds``, ``is_slow_machine``, ``warning_message``, ``is_cached_template``,
            and ``docker_image_size`` (image size in bytes, or ``None`` if unknown).
        """
        resp = self._request(
            "GET",
            "/executors/deployment-estimate",
            params={"executor_id": executor_id, "template_id": template_id},
        )
        return resp.json()

    def balance(self) -> float:
        """Get current account balance.

        Returns:
            Floating-point balance value reported by ``/users/me``.
        """
        return float(self._request("GET", "/users/me").json().get("balance") or 0)

    def events(
        self,
        *,
        since: Optional[datetime] = None,
        pod_id: Optional[str] = None,
        api_key_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """The account's event log, newest first (``GET /users/me/events``).

        Each entry is one recorded action — a rent request, creation, reboot, deletion with its
        reason, an API/SSH key or template change — with ``actor`` naming the session or API key
        (``api_key_id``, ``api_key_name``) that made the request; ``null`` when the platform acted
        by itself. ``pod_id`` also answers for a pod that has since been deleted.
        """
        params: Dict[str, Any] = {"limit": limit}
        if since is not None:
            params["since"] = since.isoformat()
        if pod_id:
            params["pod_id"] = pod_id
        if api_key_id:
            params["api_key_id"] = api_key_id
        data = self._request("GET", "/users/me/events", params=params).json()
        return data if isinstance(data, list) else []

    def audit_log(
        self,
        *,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        action: Optional[str] = None,
        source: Optional[str] = None,
        api_key_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 100,
    ) -> Dict[str, Any]:
        """One page of the account audit log, newest first (``GET /account/audit``, lium-platform DAH-3245).

        One entry per request that changed something on the account — a pod created, restarted or
        deleted, a key created or revoked, a login, a top-up requested, a setting or a workspace member
        changed — with ``action`` (``pod.delete``, ``key.create``, ``login``, …), ``actor`` (``auth``
        ``session`` or ``api_key``, ``user_id``, ``api_key_id``, ``api_key_name``), ``source`` (``portal``,
        ``cli``, ``sdk``, ``mcp``, ``admin``, ``api``), ``ip`` (filled on the caller's own entries only),
        ``user_agent``, ``request_id``, ``method``, ``route``, ``status_code``, ``resource_type``,
        ``resource_id``, ``summary``. ``action`` filters by prefix (``pod.`` is every pod action).
        Returns ``{"items": [...], "next_cursor": ...}``; pass ``next_cursor`` back as ``cursor`` for the
        next (older) page, ``None`` when this page was the last. ``limit`` is 1–500. A key needs the
        ``read`` scope; a server without the route answers 404 (``LiumNotFoundError``).
        """
        params: Dict[str, Any] = {"limit": limit}
        if since is not None:
            params["since"] = since.isoformat()
        if until is not None:
            params["until"] = until.isoformat()
        for name, value in (
            ("action", action),
            ("source", source),
            ("api_key_id", api_key_id),
            ("resource_id", resource_id),
            ("cursor", cursor),
        ):
            if value:
                params[name] = value
        data = self._request("GET", "/account/audit", params=params).json()
        if not isinstance(data, dict):
            return {"items": [], "next_cursor": None}
        items = data.get("items")
        return {"items": items if isinstance(items, list) else [], "next_cursor": data.get("next_cursor")}

    def topup_currencies(self, refresh: bool = False) -> List[Dict[str, Any]]:
        """List stablecoin currencies/networks supported for self-serve top-ups.

        Args:
            refresh: Bypass the server-side cache and re-fetch from the provider.

        Returns:
            List of ``{"code", "network", "decimals", "display_decimals"}`` dicts.
        """
        params = {"refresh": "true"} if refresh else None
        data = self._request("GET", "/tmc-pay/currencies", params=params).json()
        return data.get("currencies", [])

    def topup_create_invoice(
        self, amount: float, crypto_currency: str, crypto_network: str
    ) -> Dict[str, Any]:
        """Create a stablecoin top-up invoice for the current account.

        The returned ``deposit_address`` is where the exact ``crypto_amount`` of
        ``crypto_currency`` (on ``crypto_network``) must be sent. Once the provider
        confirms the transfer, the account balance is credited automatically.

        Args:
            amount: Top-up amount in USD.
            crypto_currency: Stablecoin code (e.g. ``"USDT"``), see :meth:`topup_currencies`.
            crypto_network: Network the stablecoin is sent on (e.g. ``"tron"``).

        Returns:
            Invoice dict including ``invoice_id``, ``deposit_address``, ``crypto_amount``,
            ``crypto_currency``, ``crypto_network``, ``exchange_rate`` and ``expires_at``.
        """
        payload = {
            "amount": amount,
            "crypto_currency": crypto_currency,
            "crypto_network": crypto_network,
        }
        return self._request("POST", "/tmc-pay/create-invoice", json=payload).json()

    def volumes(self) -> List[VolumeInfo]:
        """List all volumes for the current user.

        Returns:
            List of :class:`VolumeInfo`.
        """
        data = self._request("GET", "/volumes").json()
        return [self._dict_to_volume_info(v) for v in data]

    def volume(self, volume_id: str) -> VolumeInfo:
        """Get a specific volume by ID.

        Args:
            volume_id: Volume identifier.

        Returns:
            :class:`VolumeInfo` for the requested volume.
        """
        response = self._request("GET", f"/volumes/{volume_id}").json()
        return self._dict_to_volume_info(response)

    def volume_create(self, name: str, *, description: str = "") -> VolumeInfo:
        """Create a new volume.

        Args:
            name: Volume name.
            description: Optional description.

        Returns:
            Created :class:`VolumeInfo`.
        """
        payload = {"name": name, "description": description}
        response = self._request("POST", "/volumes", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_update(self, volume_id: str, *, name: Optional[str] = None, description: Optional[str] = None) -> VolumeInfo:
        """Update a volume's metadata.

        Args:
            volume_id: Volume identifier.
            name: Optional new name.
            description: Optional description.

        Returns:
            Updated :class:`VolumeInfo`.

        Raises:
            ValueError: If neither ``name`` nor ``description`` is provided.
        """
        payload = {}
        if name is not None:
            payload["name"] = name
        if description is not None:
            payload["description"] = description
        if not payload:
            raise ValueError("At least one of name or description must be provided")
        response = self._request("PUT", f"/volumes/{volume_id}", json=payload).json()
        return self._dict_to_volume_info(response)

    def volume_delete(self, volume_id: str) -> Dict[str, Any]:
        """Delete a volume.

        Args:
            volume_id: Volume identifier.

        Returns:
            API response payload from the delete request.
        """
        return self._request("DELETE", f"/volumes/{volume_id}").json()

    def schedule_termination(self, pod: Union[str, PodInfo, Dict], *, termination_time: str) -> Dict[str, Any]:
        """Schedule a pod for automatic termination at a future date and time.

        The pod does not have to be running: the dict :meth:`up` returns, or its ``id``,
        is enough, so the schedule can be set before :meth:`wait_ready` — a pod that never
        becomes ready is billed all the same and is removed at ``termination_time``.

        Args:
            pod: Pod identifier, PodInfo (or any object with an ``id`` attribute), or dict with an ``id`` field.
            termination_time: ISO 8601 formatted datetime string (e.g., "2025-10-17T15:30:00Z")

        Returns:
            Response from the schedule termination API
        """
        pod_id = pod["id"] if isinstance(pod, dict) else getattr(pod, "id", pod)
        payload = {"removal_scheduled_at": termination_time}
        # Idempotent payload: the same removal time twice is one schedule, so a 5xx or a lost response
        # is retried — one blip after `lium up --ttl` must not leave the pod without its auto-stop.
        return self._request("POST", f"/pods/{quote(str(pod_id), safe='')}/schedule-removal", json=payload, retry=True).json()

    def cancel_scheduled_termination(self, pod: PodInfo) -> Dict[str, Any]:
        """Cancel a scheduled termination for a pod.

        Args:
            pod: Pod to cancel the schedule for

        Returns:
            Response from the cancel scheduled termination API
        """
        return self._request("DELETE", f"/pods/{pod.id}/schedule-removal").json()

    def install_jupyter(self, pod: PodInfo, *, jupyter_internal_port: int) -> Dict[str, Any]:
        """Install Jupyter Notebook on a pod.

        Args:
            pod: Pod to install Jupyter on
            jupyter_internal_port: Internal port for Jupyter Notebook

        Returns:
            Response from the install Jupyter API
        """
        payload = {"jupyter_internal_port": jupyter_internal_port}
        return self._request("POST", f"/pods/{pod.id}/install-jupyter", json=payload).json()
