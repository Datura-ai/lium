"""Persisted JWT cache with concurrency safety (A5).

Two ``lium provider ...`` processes may race through 401 -> re-login. Without
locking the second process clobbers the first's token. Without atomic
writes a crashed write leaves a half-truncated JSON.

This module:

- writes via ``<path>.tmp`` + ``os.replace`` (atomic on POSIX)
- wraps load+refresh in ``fcntl.flock(LOCK_EX | LOCK_NB)``
- on contention raises ``ProviderError(code=PORTAL_AUTH_REFRESH_RACE)``
  (exit code 7) so the caller can back off and retry
- enforces ``0o600`` on every save
- decodes JWT ``exp`` via ``PyJWT`` (``options={"verify_signature": False}``)
  so we can detect expiry locally without the portal's JWT secret
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import jwt as pyjwt

from lium.provider.errors import (
    PORTAL_AUTH_REFRESH_RACE,
    ProviderError,
)

DEFAULT_TOKEN_PATH = Path.home() / ".lium" / "provider-portal-token.json"


@dataclass(frozen=True)
class CachedToken:
    """A JWT plus the metadata stored alongside it."""

    token: str
    exp: int  # unix seconds
    provider_id: str | None = None
    hotkey: str = ""

    def expired(self, *, leeway_seconds: int = 30, now: int | None = None) -> bool:
        """Return True if the token has expired or is within ``leeway_seconds`` of doing so."""
        current = int(time.time()) if now is None else now
        return current + leeway_seconds >= self.exp


class TokenStore:
    """JSON file holding ``{hotkey: {token, exp, provider_id}}`` records."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else DEFAULT_TOKEN_PATH

    # ------------------------------------------------------------------
    # Public API

    def load(self, hotkey: str) -> CachedToken | None:
        """Return the cached token for ``hotkey`` if present and not expired."""
        with self._locked():
            data = self._read_all()
            entry = data.get(hotkey)
            if not entry:
                return None
            try:
                cached = CachedToken(
                    token=str(entry["token"]),
                    exp=int(entry["exp"]),
                    provider_id=entry.get("provider_id"),
                    hotkey=hotkey,
                )
            except (KeyError, TypeError, ValueError):
                # Corrupted entry -- treat as missing.
                return None
            if cached.expired():
                return None
            return cached

    def save(
        self, hotkey: str, token: str, *, provider_id: str | None = None
    ) -> CachedToken:
        """Persist a token; returns the resulting :class:`CachedToken`."""
        exp = _decode_jwt_exp(token)
        with self._locked():
            data = self._read_all()
            data[hotkey] = {"token": token, "exp": exp, "provider_id": provider_id}
            self._write_all(data)
        return CachedToken(token=token, exp=exp, provider_id=provider_id, hotkey=hotkey)

    def clear(self, hotkey: str | None = None) -> None:
        """Remove ``hotkey`` (or all entries) from the cache."""
        with self._locked():
            if not self.path.exists():
                return
            if hotkey is None:
                self._write_all({})
                return
            data = self._read_all()
            if hotkey in data:
                del data[hotkey]
                self._write_all(data)

    # ------------------------------------------------------------------
    # Internals

    def _read_all(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                content = f.read()
            if not content.strip():
                return {}
            data = json.loads(content)
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_all(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        # Write 0600 explicitly: open with O_CREAT | O_WRONLY | O_TRUNC and mode 0600.
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True, indent=2)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        try:
            os.replace(tmp, self.path)
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        # Belt-and-braces: enforce mode in case umask widened it.
        with contextlib.suppress(OSError):
            os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Acquire an exclusive non-blocking flock on the cache file.

        Implementation note: we lock a sibling lockfile rather than the cache
        itself because the cache file may not exist yet, and locking a file
        we then ``os.replace`` would invalidate the lock fd mid-operation.
        """
        # POSIX-only. On Windows ``fcntl`` is unavailable; we degrade to no
        # locking with a warning. The plan's audience is dev-machine MVP on
        # Unix-likes (per spec), so this is acceptable in v1.
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback
            yield
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        # Open a fresh fd each time; do not cache because the contract is
        # one critical-section per call.
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as e:
                raise ProviderError(
                    "another lium provider process is using the token cache",
                    code=PORTAL_AUTH_REFRESH_RACE,
                    cause=e,
                    context={"lock_path": str(lock_path)},
                ) from e
            yield
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                os.close(fd)


def _decode_jwt_exp(token: str) -> int:
    """Extract the ``exp`` claim from a JWT without verifying the signature.

    The portal owns the secret; we only need to know when the token has
    expired locally so we don't waste a network round-trip on a known-dead
    JWT. If the token has no ``exp`` claim we fall back to ``now + 1 week``
    matching the portal's documented JWT lifetime.
    """
    try:
        claims = pyjwt.decode(
            token, options={"verify_signature": False, "verify_exp": False}
        )
    except pyjwt.PyJWTError:
        return int(time.time()) + 7 * 24 * 3600
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        return int(exp)
    return int(time.time()) + 7 * 24 * 3600


def with_refresh_retry(
    func: "Any",
    *,
    max_retries: int = 3,
    delay_range: tuple[float, float] = (0.1, 0.3),
) -> "Any":
    """Call ``func()``; on ``PORTAL_AUTH_REFRESH_RACE`` back off and retry.

    Used by callers that wrap a load+refresh sequence and want to hide
    transient contention from the user.
    """
    last_err: ProviderError | None = None
    for _ in range(max_retries):
        try:
            return func()
        except ProviderError as e:
            if e.code != PORTAL_AUTH_REFRESH_RACE:
                raise
            last_err = e
            time.sleep(random.uniform(*delay_range))
    if last_err is not None:
        raise last_err
    return None  # pragma: no cover - unreachable


__all__ = [
    "DEFAULT_TOKEN_PATH",
    "CachedToken",
    "TokenStore",
    "with_refresh_retry",
]
