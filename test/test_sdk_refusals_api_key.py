"""GET /keys/{id}/refusals answers an API key asking about itself (authenticateApiKeyDeps on the route), so
refusals() sends the key when there is no browser session."""

import pytest

from lium.sdk import Config, Lium, LiumSessionError


def test_refusals_is_sent_with_the_api_key_when_there_is_no_session(monkeypatch):
    lium = Lium(Config(api_key="k", session_token=None))
    sent = []

    class _Resp:
        def json(self):
            return []

    monkeypatch.setattr(lium, "_request", lambda method, endpoint, **kw: sent.append((method, endpoint)) or _Resp())

    try:
        lium.api_keys.refusals("key-1")
    except LiumSessionError as exc:
        pytest.fail(f"refused client-side: {exc}")

    assert sent == [("GET", "/keys/key-1/refusals")]
