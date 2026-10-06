"""What `lium whoami` knows about the local setup and the account."""

import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from lium.__about__ import __version__
from lium.sdk import Lium, LiumError
from lium.sdk.config import Config, api_key_fingerprint, resolve_api_key


def local_ssh_key_path() -> Optional[Path]:
    """The private key the CLI would use: the configured path, else the SDK's default search."""
    from lium.cli.settings import config

    configured = config.get("ssh.key_path")
    if configured:
        return Path(configured).expanduser()
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        candidate = Path.home() / ".ssh" / name
        if candidate.exists():
            return candidate
    return None


def public_key_material(private_key_path: Optional[Path]) -> Optional[str]:
    """``<type> <base64>`` of the matching ``.pub`` file, comment stripped; None if unreadable."""
    if private_key_path is None:
        return None
    pub = private_key_path.with_suffix(private_key_path.suffix + ".pub")
    if not pub.exists():
        return None
    for line in pub.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
            return f"{parts[0]} {parts[1]}"
    return None


def ssh_key_is_registered(lium: Lium, material: Optional[str]) -> Optional[bool]:
    """Whether the local public key is on the account. None when it cannot be told."""
    if not material:
        return None
    registered = set()
    for key in lium.list_ssh_keys():
        parts = (key.public_key or "").split()
        if len(parts) >= 2:
            registered.add(f"{parts[0]} {parts[1]}")
    return material in registered


@dataclass
class Identity:
    """One row of everything an operator asks first when something is off."""

    cli_version: str
    api_key_fingerprint: str
    api_key_source: Optional[str]
    api_base_url: Optional[str] = None
    api_reachable: Optional[bool] = None
    api_latency_ms: Optional[int] = None
    api_error: Optional[str] = None
    account_id: Optional[str] = None
    email: Optional[str] = None
    balance_usd: Optional[float] = None
    ssh_key_path: Optional[str] = None
    ssh_public_key_found: bool = False
    ssh_key_registered: Optional[bool] = None
    ssh_client: Optional[str] = None
    rsync_client: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # The SDK error the API answered with (401, 403, 5xx…), kept so `whoami` fails the way every
    # other command fails on it; ``api_error`` carries its text. Not part of the JSON payload.
    api_exception: Optional[LiumError] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("api_exception", None)
        return payload

    @property
    def has_api_key(self) -> bool:
        return self.api_key_fingerprint != "none"


def collect_identity(lium_factory=None) -> Identity:
    """Gather the identity without raising: whatever fails becomes a field, not an exception."""
    lium_factory = lium_factory or Lium
    api_key, source = resolve_api_key()
    identity = Identity(
        cli_version=__version__,
        api_key_fingerprint=api_key_fingerprint(api_key),
        api_key_source=source,
        ssh_client=shutil.which("ssh"),
        rsync_client=shutil.which("rsync"),
    )

    key_path = local_ssh_key_path()
    identity.ssh_key_path = str(key_path) if key_path else None
    material = public_key_material(key_path)
    identity.ssh_public_key_found = material is not None

    if not api_key:
        identity.warnings.append("No API key: set LIUM_API_KEY or run 'lium init'")
        return identity

    try:
        # The effective config, so LIUM_BASE_URL / LIUM_PAY_URL apply here as in every other command.
        config = Config.load()
        config.ssh_key_path = key_path
        lium = lium_factory(config)
    except Exception as exc:  # noqa: BLE001 - report, do not crash a diagnostic
        identity.api_error = str(exc)
        identity.api_reachable = False
        return identity
    # The key the commands actually run with: under a stored workspace default or LIUM_WORKSPACE
    # that is the `[workspace.<name>]` key, not `[api] api_key` — the same one auth errors name.
    identity.api_key_fingerprint = api_key_fingerprint(config.api_key)
    identity.api_key_source = config.api_key_source
    identity.api_base_url = lium.config.base_url

    started = time.monotonic()
    try:
        me = lium.me()
    except LiumError as exc:
        # The API answered (401, 403, 429, 5xx): reachable, but the call failed. The error is kept
        # whole so the command can raise it the way every other command does (invalid_api_key,
        # permission_denied…) instead of calling the API "unreachable".
        identity.api_reachable = True
        identity.api_latency_ms = int((time.monotonic() - started) * 1000)
        identity.api_error = str(exc)
        identity.api_exception = exc
        return identity
    except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeouts: all "not reachable"
        identity.api_reachable = False
        identity.api_error = f"{type(exc).__name__}: {exc}"
        return identity
    identity.api_reachable = True
    identity.api_latency_ms = int((time.monotonic() - started) * 1000)
    identity.account_id = str(me.get("id")) if me.get("id") is not None else None
    identity.email = me.get("email")
    try:
        identity.balance_usd = float(me.get("balance") or 0)
    except (TypeError, ValueError):
        identity.balance_usd = None

    try:
        identity.ssh_key_registered = ssh_key_is_registered(lium, material)
    except Exception as exc:  # noqa: BLE001 - listing keys is optional; the warning below is the report
        identity.warnings.append(f"Could not list registered SSH keys: {exc}")

    return identity
