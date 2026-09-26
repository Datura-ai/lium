"""Renter secrets: named values for pods, kept out of templates, argv and listings.

Experimental and off by default: nothing here is reachable unless ``LIUM_SECRETS_ENABLED=1``. The
server endpoints are not live yet; this client is written against the API shape below:

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
SECRET_NAME_MAX = 64
SECRET_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,%d}" % (SECRET_NAME_MAX - 1))
SECRETS_DISABLED = f"Secrets are experimental: set {SECRETS_FLAG_ENV}=1 to use them"

# Prefixes that issuers put in front of a credential (matched case-insensitively, `-` and `_` alike).
# A name that starts with one is refused only when what follows looks random, so HF_TOKEN,
# GITHUB_PAT_NAME or SK_LIVE_KEY stay valid names.
CREDENTIAL_PREFIXES = (
    "github_pat_", "ghp_", "gho_", "ghs_", "ghu_", "ghr_",  # GitHub
    "hf_",  # Hugging Face
    "xoxa_", "xoxb_", "xoxp_", "xoxr_", "xoxs_", "xapp_",  # Slack
    "sk_", "rk_", "pk_live_", "pk_test_",  # OpenAI / Anthropic / Stripe
    "glpat_", "gldt_",  # GitLab
    "npm_", "pypi_", "gsk_", "r8_", "xai_", "dop_v1_",  # npm, PyPI, Groq, Replicate, xAI, DigitalOcean
)
AWS_ACCESS_KEY_ID = re.compile(r"(?:AKIA|ASIA|AGPA|AIDA|AROA)[A-Z0-9]{16}")
LOOKS_LIKE_A_VALUE = (
    "That looks like a secret value, not a name (not shown); pass it as the value, not the name: "
    "lium secrets set NAME and enter the value when prompted"
)


def _character_classes(segment: str) -> int:
    return sum((any(c.isupper() for c in segment), any(c.islower() for c in segment), any(c.isdigit() for c in segment)))


def _looks_random(segment: str, min_length: int) -> bool:
    """Long, and mixing case or letters with digits: how a token reads, not how a word does."""
    if len(segment) < min_length:
        return False
    classes = _character_classes(segment)
    return classes == 3 or (classes == 2 and len(segment) >= min_length + 4)


def looks_like_a_credential(name: str) -> bool:
    """Whether a would-be name reads like a pasted token (hf_…, ghp_…, AKIA…, a long random run)."""
    if len(name) > SECRET_NAME_MAX or AWS_ACCESS_KEY_ID.fullmatch(name):
        return True
    normalized = name.replace("-", "_")
    lowered = normalized.lower()
    for prefix in CREDENTIAL_PREFIXES:
        if lowered.startswith(prefix):
            rest = normalized[len(prefix):]
            if any(_looks_random(part, 8) for part in rest.split("_")):
                return True
            if "_" not in rest and len(rest) >= 16:
                return True
    return any(_looks_random(part, 20) for part in normalized.split("_"))


def secrets_enabled() -> bool:
    return os.environ.get(SECRETS_FLAG_ENV, "").strip().lower() in ("1", "true", "yes")


def require_secrets_enabled() -> None:
    if not secrets_enabled():
        raise LiumError(SECRETS_DISABLED)


def invalid_secret_name_message(name: object) -> str:
    """Why a name was refused, repeating no part of it.

    A rejected name may be a pasted value: `HF=hf_...`, or a base64 token whose `=` padding makes
    everything before it look like a name. So the message never depends on more than whether an
    `=` is present.
    """
    if isinstance(name, str) and "=" in name:
        return (
            "Secret names can't contain '=' (the name was not shown: it may hold a value); pass the value "
            "separately (lium secrets set NAME and enter the value when prompted)"
        )
    if isinstance(name, str) and looks_like_a_credential(name):
        return LOOKS_LIKE_A_VALUE
    return (
        "Invalid secret name (not shown: it may hold a value): letters, digits and _ only, "
        f"not starting with a digit, at most {SECRET_NAME_MAX}"
    )


def validate_secret_name(name: str) -> str:
    if not isinstance(name, str) or not SECRET_NAME_PATTERN.fullmatch(name) or looks_like_a_credential(name):
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

    TODO(client-side encryption): today the value is sent as-is over TLS (``encryption: none``).
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
