"""Utility helpers for the Lium SDK."""

import hashlib
import random
import re
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Callable, Optional, TypeVar
from urllib.parse import urljoin, urlparse

import requests

from .exceptions import LiumError, LiumRateLimitError, LiumServerError

F = TypeVar("F", bound=Callable[..., object])

# Human-friendly ID parts
ADJECTIVES = ["swift", "brave", "calm", "eager", "gentle", "cosmic", "golden", "lunar", "zesty", "noble"]
NOUNS = ["hawk", "lion", "eagle", "fox", "wolf", "shark", "raven", "matrix", "comet", "orbit"]


def parse_api_timestamp(value: Optional[str]) -> Optional[datetime]:
    """An API timestamp as an aware UTC datetime; None when missing or unparseable."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def spend_cap_deadline(started_at: datetime, price_per_hour: float, budget_usd: float) -> datetime:
    """When a rental billed at ``price_per_hour`` from ``started_at`` has spent ``budget_usd``.

    Billing is per hour of wall time from creation, so a budget is a deadline:
    ``started_at + budget / price``. There is no server-side spend cap yet; the
    deadline is enforced by scheduling the pod's removal for that time.

    Raises:
        ValueError: A non-positive budget, or an unknown/zero price (the deadline
            would be "never", which is not a cap).
    """
    if budget_usd <= 0:
        raise ValueError(f"Budget must be positive, got {budget_usd}")
    if not price_per_hour or price_per_hour <= 0:
        raise ValueError("Cannot cap spend without a positive hourly price")
    return started_at + timedelta(hours=budget_usd / price_per_hour)


def generate_huid(id_str: str) -> str:
    """Generate human-readable ID from UUID."""
    if not id_str:
        return "invalid"

    digest = hashlib.md5(id_str.encode()).hexdigest()
    adj = ADJECTIVES[int(digest[:4], 16) % len(ADJECTIVES)]
    noun = NOUNS[int(digest[4:8], 16) % len(NOUNS)]
    return f"{adj}-{noun}-{digest[-2:]}"


# Short names users type that are not literally the extracted type.
GPU_TYPE_ALIASES = {
    "PRO6000": "RTXPRO6000",
    "RTX6000PRO": "RTXPRO6000",
    "6000PRO": "RTXPRO6000",
    "PRO6000D": "RTXPRO6000D",
}


def extract_gpu_type(machine_name: str) -> str:
    """Extract GPU type from machine name.

    Examples:
        "NVIDIA H100 80GB HBM3"                            -> "H100"
        "NVIDIA GeForce RTX 4090"                          -> "RTX4090"
        "NVIDIA RTX 6000 Ada Generation"                   -> "RTX6000"
        "NVIDIA RTX PRO 6000 Blackwell Server Edition"     -> "RTXPRO6000"
        "NVIDIA RTX PRO 6000 Blackwell Workstation Edition"-> "RTXPRO6000"
        "NVIDIA RTX PRO 6000D Blackwell Workstation Edition"-> "RTXPRO6000D"
        "NVIDIA A100-SXM4-80GB"                            -> "A100"
    """
    patterns = [
        # "RTX PRO 6000 Blackwell ..." must be tried before the plain RTX pattern, otherwise the
        # word "PRO" breaks the match and the type falls through to the last word ("Edition").
        # The suffix stays: "RTX PRO 6000D" (Blackwell, 84 GB) is a separate SKU from the 96 GB "RTX PRO 6000".
        (r"RTX\s*PRO\s*(\d{4}D?)", lambda m: f"RTXPRO{m.group(1)}"),
        (r"RTX\s*(\d{4})", lambda m: f"RTX{m.group(1)}"),
        (r"([HBL])(\d{2,3}S?)", lambda m: f"{m.group(1)}{m.group(2)}"),
        (r"A(\d{2,4})", lambda m: f"A{m.group(1)}"),
    ]
    for pattern, fmt in patterns:
        if match := re.search(pattern, machine_name, re.I):
            return fmt(match).upper()
    words = machine_name.split() if machine_name else []
    return words[-1] if words else "Unknown"


def normalize_gpu_short(gpu_short: str) -> str:
    """Canonical form of a user-typed GPU short name for comparisons.

    Upper-cases, drops spaces/hyphens/underscores and applies :data:`GPU_TYPE_ALIASES`,
    so ``"rtx pro 6000"``, ``"RTX-PRO-6000"``, ``"pro6000"`` all become ``"RTXPRO6000"``.
    """
    key = re.sub(r"[\s_\-]+", "", gpu_short or "").upper()
    return GPU_TYPE_ALIASES.get(key, key)


def gpu_short_matches(gpu_short: str, gpu_type: str) -> bool:
    """Whether a user-typed short name names the GPU type extracted from a machine name.

    Exact after normalisation (``"rtx 4090"`` → ``RTX4090``), or a bare model number
    naming the type's number: ``"4090"`` matches ``RTX4090``, ``"6000"`` matches
    ``RTX6000``, ``RTXPRO6000`` and ``A6000`` alike (the caller gets every match).
    The number has to match whole: ``"100"`` matches ``H100`` and ``A100`` (both are
    "the 100s"), while ``"90"`` matches nothing.
    """
    wanted = normalize_gpu_short(gpu_short)
    # both sides normalised (DAH-2903): a fall-through name keeps its casing ("Ti", "Xp") and `--gpu ti` must still match
    have = normalize_gpu_short(gpu_type)
    if not wanted or not have:
        return False
    if wanted == have:
        return True
    if wanted.isdigit():
        return _trailing_model_number(have) == wanted
    return False


def _trailing_model_number(gpu_type: str) -> str:
    """The digit run that ends a normalised type, after an optional letter suffix: ``RTX4090`` → ``4090``,
    ``RTXPRO6000D`` → ``6000``, ``H100SXM`` → ``100``, ``UNKNOWN`` → ``""``.

    Two slices, no regex: ``gpu_type`` falls through from an API-supplied machine name of any length, and a
    backtracking pattern on it was polynomial (PR_PROCESS §5, linear regex on wire-derived text).
    """
    stem = gpu_type.rstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    end = len(stem)
    start = end
    while start and stem[start - 1].isdigit():
        start -= 1
    return stem[start:end]


def expand_gpu_shorthand(gpu_short: str) -> str:
    """Expand GPU shorthand to a pattern that matches full machine names.

    Examples:
        A100 -> "A100" (matches "NVIDIA A100-SXM4-80GB", "NVIDIA A100-PCIE-40GB", etc.)
        H200 -> "H200" (matches "NVIDIA H200", etc.)
        RTX4090 -> "RTX 4090" (matches "NVIDIA GeForce RTX 4090", etc.)

    Args:
        gpu_short: Short GPU name like "A100", "H200", "RTX4090"

    Returns:
        Pattern string that can be used to filter machine names.
    """
    # Already a full name or pattern, return as-is
    if len(gpu_short) > 10 or " " in gpu_short:
        return gpu_short

    gpu_upper = gpu_short.upper()

    # Handle RTX cards - need to add space between RTX and number
    if gpu_upper.startswith("RTX"):
        # RTX4090 -> RTX 4090
        match = re.match(r"RTX(\d+)", gpu_upper)
        if match:
            return f"RTX {match.group(1)}"

    # For A-series (A100, A6000, etc.) and H-series (H100, H200, etc.)
    # Just return as-is since the API does substring matching
    # "A100" will match "NVIDIA A100-SXM4-80GB"
    return gpu_short


TRANSIENT_ERRORS = (LiumRateLimitError, LiumServerError, requests.RequestException)


def with_retry(max_attempts: int = 3, delay: float = 1.0, exceptions: tuple = TRANSIENT_ERRORS):
    """Retry decorator for API calls: back off and repeat on ``exceptions``."""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions:
                    if attempt == max_attempts - 1:
                        raise
                    time.sleep(delay * (2 ** attempt) + random.uniform(0, 0.5))
        return wrapper  # type: ignore[misc]
    return decorator


# Redirects (DAH-3543). `requests` drops only `Authorization` when a redirect changes host: a key in `X-API-KEY`, a
# portal Bearer token and a 307/308 body (the signup password) travel on to whatever host a `Location` names. Every
# HTTP path in this package sends with `allow_redirects=False` and lets `request_same_origin` decide: a redirect that
# keeps scheme, host and port is followed (at most MAX_REDIRECTS hops); any other raises LiumError.
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5


def redirect_location(resp: object) -> Optional[str]:
    """The ``Location`` of a redirect response, or ``None`` for anything else (including test doubles without headers)."""
    if getattr(resp, "status_code", None) not in REDIRECT_CODES:
        return None
    headers = getattr(resp, "headers", None) or {}
    location = headers.get("Location") or headers.get("location")
    return str(location) if location else None


def origin(url: str) -> str:
    """``scheme://host[:port]`` of ``url``, the part a redirect must keep for credentials to travel with it."""
    parts = urlparse(url)
    scheme = parts.scheme.lower()
    try:
        port = parts.port or {"https": 443, "http": 80}.get(scheme, "")
    except ValueError:  # a port that is not a number: keep the netloc as written so it never equals a real origin
        return f"{scheme}://{parts.netloc.lower()}"
    return f"{scheme}://{(parts.hostname or '').lower()}:{port}"


def host_label(url: str) -> str:
    """``scheme://host[:port]`` as written in ``url``, for error text (a credential is never in a message)."""
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def request_same_origin(
    send: Callable[..., requests.Response], method: str, url: str, **kwargs
) -> requests.Response:
    """Send ``send(method, url, allow_redirects=False, **kwargs)`` and follow only same-origin redirects.

    ``send`` is ``requests.request``, a ``Session.request`` or a test double with that signature. The method
    rewrite is the one ``requests`` applies (302/303 turn anything but HEAD into a GET, 301 only a POST; a rewritten
    request carries no body); the ``Location`` already holds the query, so ``params`` is not re-added. A redirect
    to another origin, a ``Location`` that is not a URL, or more than ``MAX_REDIRECTS`` hops raise ``LiumError``
    naming the two hosts and never the credential.
    """
    kwargs.pop("allow_redirects", None)
    for _hop in range(MAX_REDIRECTS + 1):
        resp = send(method, url, allow_redirects=False, **kwargs)
        location = redirect_location(resp)
        if location is None:
            return resp
        close = getattr(resp, "close", None)
        if callable(close):
            close()
        try:
            next_url = urljoin(url, location)
            other_origin = origin(next_url) != origin(url)
        except ValueError as exc:  # a `Location` that does not parse as a URL
            raise LiumError(f"{host_label(url)} redirected {method.upper()} to a Location that is not a URL") from exc
        if other_origin:
            raise LiumError(
                f"{host_label(url)} redirected {method.upper()} {urlparse(url).path} to {host_label(next_url)}; "
                "the API key is sent only to the configured API host, so the request was not repeated there"
            )
        upper = method.upper()
        if (resp.status_code in (302, 303) and upper != "HEAD") or (resp.status_code == 301 and upper == "POST"):
            method = "GET"
            for body_key in ("json", "data", "files"):
                kwargs.pop(body_key, None)
        kwargs.pop("params", None)
        url = next_url
    raise LiumError(f"{host_label(url)} redirected {method.upper()} more than {MAX_REDIRECTS} times")


__all__ = [
    "generate_huid", "extract_gpu_type", "expand_gpu_shorthand", "normalize_gpu_short", "gpu_short_matches",
    "GPU_TYPE_ALIASES", "with_retry", "REDIRECT_CODES", "MAX_REDIRECTS", "redirect_location", "origin", "host_label",
    "request_same_origin",
]
