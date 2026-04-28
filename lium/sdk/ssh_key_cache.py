"""Local cache of SSH keys already registered with the Lium backend.

The cache lets ``Lium.up()`` skip a ``GET /ssh-keys`` round-trip on every rent
once a public key has been confirmed registered for the active API key.
"""

import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Set

from .config import Config


CACHE_FILE_NAME = "ssh_keys_cache.json"


def _cache_path() -> Path:
    return Path.home() / ".lium" / CACHE_FILE_NAME


def _api_key_digest(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def fingerprint(public_key: str) -> str:
    """Return ``SHA256:<base64>`` fingerprint of an OpenSSH public key.

    Matches the format produced by ``ssh-keygen -lf``. Falls back to the
    SHA256 hex of the raw line if the key is malformed (so cache lookups
    stay deterministic instead of raising).
    """
    parts = public_key.strip().split()
    if len(parts) >= 2:
        try:
            blob = base64.b64decode(parts[1], validate=False)
            digest = hashlib.sha256(blob).digest()
            return "SHA256:" + base64.b64encode(digest).rstrip(b"=").decode("ascii")
        except (ValueError, base64.binascii.Error):
            pass
    return "SHA256:raw:" + hashlib.sha256(public_key.strip().encode("utf-8")).hexdigest()


def load_cache(config: Config) -> Set[str]:
    """Return the set of cached fingerprints for ``config``'s API key.

    If the file is missing, malformed, or stamped with a different
    ``api_key_digest`` (account switch), returns an empty set.
    """
    path = _cache_path()
    if not path.exists():
        return set()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return set()

    if not isinstance(data, dict):
        return set()
    if data.get("api_key_digest") != _api_key_digest(config.api_key):
        return set()

    fps = data.get("fingerprints", [])
    if not isinstance(fps, list):
        return set()
    return {fp for fp in fps if isinstance(fp, str)}


def save_cache(config: Config, fingerprints: Set[str]) -> None:
    """Atomically persist ``fingerprints`` for ``config``'s API key."""
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "api_key_digest": _api_key_digest(config.api_key),
        "fingerprints": sorted(fingerprints),
    }

    fd, tmp_path = tempfile.mkstemp(prefix=".ssh_keys_cache.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


__all__ = ["fingerprint", "load_cache", "save_cache"]
