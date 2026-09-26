"""Renter secrets: named values for pods, kept out of templates, argv and listings (DAH-1482).

Experimental and off by default: nothing here is reachable unless ``LIUM_SECRETS_ENABLED=1``. The
lium-platform endpoints are not built yet; this client is written against the API shape below, which
the backend half is expected to implement:

- ``GET /secrets`` -> ``[{"name": str, "updated_at": str}]`` — never a value.
- ``PUT /secrets/{name}`` with the body :func:`encrypt_for_upload` returns -> ``{"name", "updated_at"}``;
  creates or replaces.
- ``DELETE /secrets/{name}``.
- ``POST /executors/{id}/rent`` accepts ``"secret_names": [...]``; the backend resolves the values and the
  validator writes each one as a 0400 file under ``/run/lium/secrets/`` on a tmpfs in the pod.
"""

import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterable, List, Optional

from .exceptions import LiumError

if TYPE_CHECKING:  # pragma: no cover
    from .client import Lium

SECRETS_FLAG_ENV = "LIUM_SECRETS_ENABLED"
# The name becomes a file name in the pod (/run/lium/secrets/<name>) and is shown by `list`; only the value is secret.
SECRET_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
SECRETS_DISABLED = f"Secrets are experimental: set {SECRETS_FLAG_ENV}=1 to use them"


def secrets_enabled() -> bool:
    return os.environ.get(SECRETS_FLAG_ENV, "").strip().lower() in ("1", "true", "yes")


def require_secrets_enabled() -> None:
    if not secrets_enabled():
        raise LiumError(SECRETS_DISABLED)


def invalid_secret_name_message(name: object) -> str:
    """Why a name was refused, without repeating it: a rejected name may be a pasted value (`HF=hf_...`)."""
    if isinstance(name, str) and "=" in name:
        prefix = name.split("=", 1)[0]
        shown = f" ({prefix} followed by '=')" if SECRET_NAME_PATTERN.fullmatch(prefix) else ""
        return (
            f"Secret names can't contain '='{shown}; pass the value separately "
            "(lium secrets set NAME and enter the value when prompted)"
        )
    return "Invalid secret name: letters, digits and _ only, not starting with a digit, at most 128"


def validate_secret_name(name: str) -> str:
    if not isinstance(name, str) or not SECRET_NAME_PATTERN.fullmatch(name):
        raise ValueError(invalid_secret_name_message(name))
    return name


def validate_secret_names(names: Optional[Iterable[str]]) -> List[str]:
    """Names in order with duplicates dropped; raises ValueError on the first bad one."""
    seen: Dict[str, None] = {}
    for name in names or ():
        seen[validate_secret_name(name)] = None
    return list(seen)


def encrypt_for_upload(name: str, value: str) -> Dict[str, str]:
    """The request body that carries one secret value to the server; every upload goes through here.

    TODO(DAH-1482 client-side encryption): today the value is sent as-is over TLS (``encryption: none``).
    The client-side encryption half replaces this body with ciphertext and names its scheme in
    ``encryption``; no caller needs to change.
    """
    del name  # reserved for the encryption half (e.g. as associated data)
    return {"value": value, "encryption": "none"}


@dataclass
class SecretInfo:
    """A secret as listed: its name and when it last changed — never its value."""

    name: str
    updated_at: Optional[str] = None


def _info(d: Dict) -> SecretInfo:
    return SecretInfo(name=str(d.get("name", "")), updated_at=d.get("updated_at"))


class SecretsClient:
    def __init__(self, lium: "Lium"):
        self._lium = lium

    def list(self) -> List[SecretInfo]:
        require_secrets_enabled()
        return [_info(d) for d in self._lium._request("GET", "/secrets").json()]

    def set(self, name: str, value: str) -> SecretInfo:
        require_secrets_enabled()
        validate_secret_name(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"Secret {name} needs a non-empty value")
        response = self._lium._request("PUT", f"/secrets/{name}", json=encrypt_for_upload(name, value))
        body = response.json() if response.content else {}
        return _info(body) if isinstance(body, dict) and body.get("name") else SecretInfo(name=name)

    def delete(self, name: str) -> None:
        require_secrets_enabled()
        validate_secret_name(name)
        self._lium._request("DELETE", f"/secrets/{name}")


__all__ = [
    "SecretsClient",
    "SecretInfo",
    "SECRETS_FLAG_ENV",
    "encrypt_for_upload",
    "secrets_enabled",
    "validate_secret_name",
    "validate_secret_names",
]
