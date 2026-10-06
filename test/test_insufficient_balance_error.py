"""A balance refusal is its own exception, carrying the numbers — and it names the key.

A caller that wants to react to "not enough balance" (top up, pick a cheaper
executor, stop a batch) had to grep the message of a generic
`LiumPermissionError`. `LiumInsufficientBalanceError` is a subclass so the old
handlers keep working, and `.required` / `.available` are floats (USD) when the
server said what they are. One parser decides (`lium.sdk.client.permission_error`,
DAH-2886): the platform's structured `error.code` when the response carries one,
the message text otherwise; the amounts come from the message. This PR adds the
key's fingerprint and source to the message.
"""

import pytest

from lium.sdk import Config, Lium, LiumError, LiumInsufficientBalanceError, LiumPermissionError

BALANCE_MESSAGE = (
    "Insufficient balance. This node costs $2.00/hour, so renting it requires at least $12.50 "
    "(15 minutes of runtime). Your balance is $3.00."
)


class _Response:
    ok = False
    text = ""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


def _client_receiving(monkeypatch, response):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: response)
    return Lium(Config(api_key="k-0123456789abcdef", api_key_source="env:LIUM_API_KEY"))


def test_it_is_a_permission_error_and_a_lium_error():
    assert issubclass(LiumInsufficientBalanceError, LiumPermissionError)
    assert issubclass(LiumInsufficientBalanceError, LiumError)


def test_amounts_default_to_none():
    error = LiumInsufficientBalanceError("nope")

    assert (error.required, error.available) == (None, None)
    assert str(error) == "nope"


def test_a_structured_403_about_balance_raises_the_subclass_with_the_amounts_and_the_key(monkeypatch):
    """lium-platform#210's body: ``error.code`` decides, the message carries the amounts."""
    client = _client_receiving(monkeypatch, _Response(403, {
        "success": False,
        "error": {"code": "insufficient_balance", "message": BALANCE_MESSAGE, "hint": "Top up", "request_id": "r-1"},
        "message": BALANCE_MESSAGE,
        "status_code": 403,
    }))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert raised.value.required == 12.5
    assert raised.value.available == 3.0
    assert "from env:LIUM_API_KEY" in str(raised.value)


def test_an_older_server_is_read_from_the_message_text(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": BALANCE_MESSAGE}))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert (raised.value.required, raised.value.available) == (12.5, 3.0)
    assert str(raised.value).endswith("(key k-0123…cdef from env:LIUM_API_KEY)")


def test_amounts_are_none_when_the_server_gives_none(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "insufficient balance"}))

    with pytest.raises(LiumInsufficientBalanceError) as raised:
        client._request("POST", "/executors/node-1/rent")

    assert (raised.value.required, raised.value.available) == (None, None)


def test_other_403s_stay_plain_permission_errors_and_still_name_the_key(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "User is not verified", "balance": 99}))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("GET", "/pods")

    assert not isinstance(raised.value, LiumInsufficientBalanceError)
    assert "User is not verified" in str(raised.value)
    assert "from env:LIUM_API_KEY" in str(raised.value)


def test_a_structured_code_other_than_insufficient_balance_is_not_a_balance_error(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {
        "error": {"code": "account_not_verified", "message": "Insufficient balance verification pending"},
        "message": "Insufficient balance verification pending",
    }))

    with pytest.raises(LiumPermissionError) as raised:
        client._request("GET", "/pods")

    assert type(raised.value) is LiumPermissionError


def test_old_handlers_catching_permission_error_still_work(monkeypatch):
    client = _client_receiving(monkeypatch, _Response(403, {"detail": "Insufficient balance"}))

    with pytest.raises(LiumPermissionError):
        client._request("POST", "/executors/node-1/rent")
