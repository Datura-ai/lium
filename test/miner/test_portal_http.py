"""Tests for ``lium.miner.portal_http`` (transport + status-code mapping)."""

from __future__ import annotations

import pytest
import requests

from lium.miner.errors import (
    PORTAL_AUTH_INVALID,
    PORTAL_CONTRACT_DRIFT,
    PORTAL_NOT_FOUND,
    PORTAL_RATE_LIMIT,
    PORTAL_SERVER_ERROR,
    MinerAuthError,
    MinerError,
    MinerNotFoundError,
    MinerPortalContractError,
    MinerServerError,
)
from lium.miner.portal_http import PortalHTTP


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by tests."""

    def __init__(
        self,
        status_code: int = 200,
        body: dict | list | str | None = None,
        text: str | None = None,
    ) -> None:
        self.status_code = status_code
        if text is not None:
            self.text = text
            self._json: object = None
            self._raise_json = True
        else:
            import json as _json

            self._body = body if body is not None else {}
            self.text = _json.dumps(self._body)
            self._json = self._body
            self._raise_json = False

    def json(self) -> object:
        if self._raise_json:
            raise ValueError("not JSON")
        return self._json


class _RecordingSession:
    """Captures the last call to ``request`` and returns a queued response."""

    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response
        self.last_call: dict | None = None

    def request(self, **kwargs: object) -> _FakeResponse:
        self.last_call = dict(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _make_http(
    response: _FakeResponse | Exception, *, token: str | None = None
) -> tuple[PortalHTTP, _RecordingSession]:
    session = _RecordingSession(response)
    http = PortalHTTP(
        base_url="https://portal.example.com",
        token_provider=lambda: token,
        session=session,  # type: ignore[arg-type]
    )
    return http, session


def test_get_2xx_returns_parsed_dict() -> None:
    http, session = _make_http(_FakeResponse(200, {"a": 1, "b": 2}), token="tok")
    assert http.get("/auth/me") == {"a": 1, "b": 2}
    assert session.last_call is not None
    assert session.last_call["method"] == "GET"
    assert session.last_call["url"] == "https://portal.example.com/auth/me"
    headers = session.last_call["headers"]
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/json"


def test_post_sends_json_body_with_correct_headers() -> None:
    http, session = _make_http(_FakeResponse(200, {"ok": True}), token="tok")
    http.post("/echo", json_body={"hello": "world"})
    assert session.last_call is not None
    assert session.last_call["json"] == {"hello": "world"}
    assert session.last_call["headers"]["Content-Type"] == "application/json"


def test_unauthenticated_request_omits_authorization_header() -> None:
    http, session = _make_http(_FakeResponse(200, {"ok": True}), token="tok")
    http.post("/auth/login-flexible", json_body={"x": 1}, auth=False)
    assert session.last_call is not None
    assert "Authorization" not in session.last_call["headers"]


def test_2xx_with_list_body_wraps_in_data() -> None:
    http, _ = _make_http(_FakeResponse(200, [1, 2, 3]))
    assert http.get("/list") == {"data": [1, 2, 3]}


def test_2xx_with_empty_body_returns_empty_dict() -> None:
    http, _ = _make_http(_FakeResponse(204, text=""))
    # 204-style empty body parses as text="" -> body=None -> {}
    assert http.get("/empty") == {}


@pytest.mark.parametrize(
    "status,exc_type,expected_code",
    [
        (401, MinerAuthError, PORTAL_AUTH_INVALID),
        (403, MinerAuthError, PORTAL_AUTH_INVALID),
        (404, MinerNotFoundError, PORTAL_NOT_FOUND),
        (422, MinerPortalContractError, PORTAL_CONTRACT_DRIFT),
        (429, MinerServerError, PORTAL_RATE_LIMIT),
        (500, MinerServerError, PORTAL_SERVER_ERROR),
        (502, MinerServerError, PORTAL_SERVER_ERROR),
    ],
)
def test_status_code_mapping(status: int, exc_type: type, expected_code: str) -> None:
    http, _ = _make_http(_FakeResponse(status, {"detail": "boom"}))
    with pytest.raises(exc_type) as exc:
        http.get("/anything")
    assert exc.value.code == expected_code
    assert exc.value.context["status"] == status
    assert exc.value.context["body"] == {"detail": "boom"}


def test_unknown_status_raises_generic_miner_error() -> None:
    http, _ = _make_http(_FakeResponse(418, {"i": "am a teapot"}))
    with pytest.raises(MinerError) as exc:
        http.get("/teapot")
    assert exc.value.context["status"] == 418


def test_network_error_raises_miner_server_error() -> None:
    http, _ = _make_http(requests.ConnectionError("dns blew up"))
    with pytest.raises(MinerServerError) as exc:
        http.get("/anything")
    assert exc.value.code == PORTAL_SERVER_ERROR
    assert "dns blew up" in exc.value.message


def test_base_url_strips_trailing_slash() -> None:
    http, session = _make_http(_FakeResponse(200, {}))
    http_alt = PortalHTTP(
        base_url="https://portal.example.com///",
        token_provider=lambda: None,
        session=session,  # type: ignore[arg-type]
    )
    http_alt.get("/x")
    assert session.last_call is not None
    assert session.last_call["url"] == "https://portal.example.com/x"


def test_path_without_leading_slash_is_normalised() -> None:
    http, session = _make_http(_FakeResponse(200, {}))
    http.get("auth/me")  # no leading slash
    assert session.last_call is not None
    assert session.last_call["url"].endswith("/auth/me")
