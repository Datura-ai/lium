"""Synchronous HTTP transport for the miner SDK.

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

from lium.miner.errors import (
    PORTAL_AUTH_EXPIRED,
    PORTAL_AUTH_INVALID,
    PORTAL_NOT_FOUND,
    PORTAL_RATE_LIMIT,
    PORTAL_SERVER_ERROR,
    MinerAuthError,
    MinerError,
    MinerNotFoundError,
    MinerPortalContractError,
    MinerServerError,
)
from lium.sdk.utils import with_retry

logger = logging.getLogger("lium.miner.portal_http")


# Default base URL: production portal API. Note: ``provider.lium.io`` is the
# Next.js frontend; the FastAPI backend lives at a separate host with no
# ``/api`` prefix. Override with ``LIUM_PORTAL_URL`` / ``--portal-url`` /
# ``miner.portal_url`` in ~/.lium/config.ini.
DEFAULT_PORTAL_URL = "https://provider-api.lium.io"


TokenProvider = Callable[[], str | None]


class PortalHTTP:
    """Thin wrapper around ``requests.Session`` for miner-portal calls.

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
            response = self._session.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self._timeout,
            )
        except requests.RequestException as e:
            # Network-level failure: with_retry will retry; on the final
            # attempt the exception bubbles up. Wrap into MinerError.
            raise MinerServerError(
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
    """Translate an HTTP response into either a parsed dict or a MinerError.

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

    if status == 401:
        raise MinerAuthError(
            "portal rejected credentials",
            code=PORTAL_AUTH_INVALID,
            context=context,
        )
    if status == 403:
        raise MinerAuthError(
            "portal forbade the requested action",
            code=PORTAL_AUTH_INVALID,
            context=context,
        )
    if status == 404:
        raise MinerNotFoundError(
            "portal returned 404",
            code=PORTAL_NOT_FOUND,
            context=context,
        )
    if status == 419 or status == 440:
        # Some portals use these for session expiry.
        raise MinerAuthError(
            "portal session expired",
            code=PORTAL_AUTH_EXPIRED,
            context=context,
        )
    if status == 422:
        raise MinerPortalContractError(
            "portal rejected payload (likely schema drift)",
            context=context,
        )
    if status == 429:
        raise MinerServerError(
            "portal rate limit",
            code=PORTAL_RATE_LIMIT,
            context=context,
        )
    if 500 <= status < 600:
        raise MinerServerError(
            f"portal server error ({status})",
            code=PORTAL_SERVER_ERROR,
            context=context,
        )
    # Anything else: treat as a generic MinerError but keep context.
    raise MinerError(
        f"unexpected portal status {status}",
        code=PORTAL_SERVER_ERROR,
        context=context,
    )


__all__ = ["DEFAULT_PORTAL_URL", "PortalHTTP", "TokenProvider"]
