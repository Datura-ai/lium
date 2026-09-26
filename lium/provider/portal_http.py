"""Synchronous HTTP transport for the provider SDK.

Mirrors the renter SDK's ``lium/sdk/client.py::_request`` style: ``requests``
+ ``with_retry`` + status-code -> structured-error mapping. One file owns
the wire so audit / redaction lives in one place.

ADR-001 chose this over OpenAPI codegen for simplicity; portal payload
drift surfaces at runtime as ``PORTAL_CONTRACT_DRIFT``.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import requests

from lium.provider.errors import (
    NET_UNREACHABLE,
    PORTAL_AUTH_EXPIRED,
    PORTAL_AUTH_INVALID,
    PORTAL_FORBIDDEN,
    PORTAL_NOT_FOUND,
    PORTAL_RATE_LIMIT,
    PORTAL_REQUEST_REJECTED,
    PORTAL_SERVER_ERROR,
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderPortalContractError,
    ProviderServerError,
)
from lium.sdk.exceptions import LiumError
from lium.sdk.utils import request_same_origin, with_retry

logger = logging.getLogger("lium.provider.portal_http")


# Default base URL: production portal API. Note: ``provider.lium.io`` is the
# Next.js frontend; the FastAPI backend lives at a separate host with no
# ``/api`` prefix. Override with ``LIUM_PORTAL_URL`` / ``--portal-url`` /
# ``provider.portal_url`` in ~/.lium/config.ini.
DEFAULT_PORTAL_URL = "https://provider-api.lium.io"


TokenProvider = Callable[[], str | None]


class PortalHTTP:
    """Thin wrapper around ``requests.Session`` for provider-portal calls.

    Args:
        base_url: the portal origin. Trailing slashes are stripped.
        token_provider: callable returning the current JWT, or ``None`` for
            unauthenticated calls. The provider is invoked on every request
            so the SDK can refresh tokens transparently.
        session: optional pre-built ``requests.Session`` (tests inject one).
        timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        token_provider: TokenProvider | None = None,
        session: requests.Session | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = (base_url or DEFAULT_PORTAL_URL).rstrip("/")
        self._token_provider = token_provider or (lambda: None)
        self._session = session or requests.Session()
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Public API

    def get(
        self, path: str, *, params: dict[str, Any] | None = None, auth: bool = True
    ) -> dict[str, Any]:
        return self._request("GET", path, params=params, auth=auth)

    def post(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        return self._request("POST", path, json_body=json_body, auth=auth)

    def put(
        self,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        return self._request("PUT", path, json_body=json_body, auth=auth)

    def delete(self, path: str, *, auth: bool = True) -> dict[str, Any]:
        return self._request("DELETE", path, auth=auth)

    # ------------------------------------------------------------------
    # Internals

    @with_retry(max_attempts=3, delay=1.0)
    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        headers: dict[str, str] = {"Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            token = self._token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"

        try:
            # Same-origin redirects only (DAH-3543): `requests` would replay the Bearer token's request
            # body and every non-Authorization header to whatever host a `Location` names.
            response = request_same_origin(
                lambda m, u, **kw: self._session.request(method=m, url=u, **kw),
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
        except LiumError as e:
            raise ProviderError(
                str(e),
                code=PORTAL_REQUEST_REJECTED,
                context={"url": url, "method": method},
            ) from e
        except (requests.ConnectionError, requests.Timeout) as e:
            # nothing answered (refused, DNS, unroutable, timed out): not a portal fault, and not a 5xx to report
            raise ProviderError(
                f"could not reach the portal at {self.base_url}: {e}",
                code=NET_UNREACHABLE,
                cause=e,
                context={"url": url, "method": method},
            ) from e
        except requests.RequestException as e:
            raise ProviderServerError(
                f"network error reaching portal: {e}",
                code=PORTAL_SERVER_ERROR,
                cause=e,
                context={"url": url, "method": method},
            ) from e

        return _parse_response(response, method=method, url=url)


def _parse_response(
    response: requests.Response,
    *,
    method: str,
    url: str,
) -> dict[str, Any]:
    """Translate an HTTP response into either a parsed dict or a ProviderError.

    Body parsing is lenient: missing/invalid JSON on a 2xx returns ``{}``;
    error bodies are stashed under ``context["body"]`` for diagnostics.
    """
    status = response.status_code

    body: Any = None
    text = response.text or ""
    if text:
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            body = text

    if 200 <= status < 300:
        if isinstance(body, dict):
            # The portal wraps single-item endpoints in
            # ``DetailResponse[T] = {success, data, timestamp}``. Unwrap so
            # SDK callers see the inner DTO directly. ``ListResponse[T]``
            # also wraps under ``data`` but adds ``total``/``pagination`` --
            # we keep its envelope intact so list callers can read those.
            if (
                body.get("success") is True
                and "data" in body
                and isinstance(body["data"], dict)
                and "total" not in body
            ):
                return body["data"]
            return body
        if body is None:
            return {}
        # Lists / scalars: wrap so callers can rely on dict shape.
        return {"data": body}

    context = {"url": url, "method": method, "status": status, "body": body}

    coded = _coded_detail(body)
    if coded is not None:
        raise _coded_error(status, coded, context)

    if status == 401:
        raise ProviderAuthError(
            "portal rejected credentials",
            code=PORTAL_AUTH_INVALID,
            context=context,
        )
    if status == 403:
        # The token is valid; the portal refused this action for this hotkey. Re-login does not help, so this is
        # not PORTAL_AUTH_INVALID (DAH-2269: machine-request detail needs a validator-verified node).
        raise ProviderAuthError(
            "portal forbade the requested action",
            code=PORTAL_FORBIDDEN,
            context=context,
        )
    if status == 404:
        raise ProviderNotFoundError(
            "portal returned 404",
            code=PORTAL_NOT_FOUND,
            context=context,
        )
    if status == 419 or status == 440:
        # Some portals use these for session expiry.
        raise ProviderAuthError(
            "portal session expired",
            code=PORTAL_AUTH_EXPIRED,
            context=context,
        )
    if status == 422:
        raise ProviderPortalContractError(
            "portal rejected payload (likely schema drift)",
            context=context,
        )
    if status == 429:
        raise ProviderServerError(
            "portal rate limit",
            code=PORTAL_RATE_LIMIT,
            context=context,
        )
    if 500 <= status < 600:
        raise ProviderServerError(
            f"portal server error ({status})",
            code=PORTAL_SERVER_ERROR,
            context=context,
        )
    if 400 <= status < 500:
        # The portal refused the request and said why (``{"detail": "Unsupported
        # gpu type."}``); that reason is the message, and it is not a 5xx to retry.
        raise ProviderError(
            f"portal rejected the request ({status}): {_portal_detail(body)}",
            code=PORTAL_REQUEST_REJECTED,
            context=context,
        )
    # Anything else: treat as a generic ProviderError but keep context.
    raise ProviderError(
        f"unexpected portal status {status}",
        code=PORTAL_SERVER_ERROR,
        context=context,
    )


def _coded_detail(body: Any) -> dict[str, Any] | None:
    """The portal's ``detail`` when it names a stable ``code`` (``{"message", "code", …}``), else None."""
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("code"), str) and detail["code"].strip():
        return detail
    return None


def _coded_error(status: int, detail: dict[str, Any], context: dict[str, Any]) -> ProviderError:
    """``portal.<detail.code>``, as the portal named it; the class still follows the status (a 401 stays an auth error)."""
    code = "portal." + detail["code"].strip()
    message = str(detail.get("message") or f"portal refused the request ({status})")
    extra = {k: v for k, v in detail.items() if k not in ("code", "message")}
    ctx = {**context, **({"detail": extra} if extra else {})}
    if status in (401, 403, 419, 440):
        return ProviderAuthError(message, code=code, context=ctx)
    if status == 404:
        return ProviderNotFoundError(message, code=code, context=ctx)
    if 500 <= status < 600:
        return ProviderServerError(message, code=code, context=ctx)
    return ProviderError(message, code=code, context=ctx)


def _portal_detail(body: Any) -> str:
    """The portal's own reason for a 4xx, flattened to one line."""
    detail = body.get("detail", body) if isinstance(body, dict) else body
    if isinstance(detail, str):
        flat = detail.strip()
    elif isinstance(detail, dict):
        flat = "; ".join(f"{k}: {v}" for k, v in detail.items())
    elif isinstance(detail, list):
        flat = "; ".join(
            str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail
        )
    else:
        flat = "" if detail is None else str(detail)
    return flat or "no detail given"


__all__ = ["DEFAULT_PORTAL_URL", "PortalHTTP", "TokenProvider"]
