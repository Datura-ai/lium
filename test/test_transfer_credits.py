"""DAH-4664: `lium transfer` and `Lium.transfer_credits` (lium-platform `POST /users/me/transfer-credits`)."""

import json
import uuid

import pytest
import requests
import responses
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli import transfer as transfer_module
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED
from lium.sdk import Config, Lium

URL = "https://lium.io/api/users/me/transfer-credits"
OK = {"sender_balance": 75.0, "transfer_id": "t-1"}


def _error(code, message, hint=""):
    # lium-platform core/exception_handlers.py error_response
    return {"success": False, "error": {"code": code, "message": message, "hint": hint, "request_id": "req-1"},
            "message": {"code": code, "message": message}}


@pytest.fixture
def client(monkeypatch):
    lium = Lium(Config(api_key="test-key"))
    monkeypatch.setattr(transfer_module, "billing_client", lambda: lium)
    return lium


@responses.activate
def test_sdk_sends_email_amount_note_and_an_idempotency_header(client):
    responses.post(URL, json=OK)

    result = client.transfer_credits("friend@example.com", 25, description="GPU hours")

    request = responses.calls[0].request
    assert json.loads(request.body) == {"receiver_email": "friend@example.com", "amount": 25, "description": "GPU hours"}
    key = request.headers["Idempotency-Key"]
    uuid.UUID(key)
    assert request.headers["X-API-KEY"] == "test-key"
    assert result == {**OK, "idempotency_key": key}


@responses.activate
def test_sdk_sends_a_user_id_as_receiver_id_with_the_callers_key(client):
    responses.post(URL, json=OK)

    client.transfer_credits("3f1c0000-0000-0000-0000-000000000001", 1.5, idempotency_key="k-1")

    request = responses.calls[0].request
    assert json.loads(request.body) == {"receiver_id": "3f1c0000-0000-0000-0000-000000000001", "amount": 1.5}
    assert request.headers["Idempotency-Key"] == "k-1"


def _run(*args, input=None):
    return CliRunner().invoke(cli, ["transfer", *args], input=input)


@responses.activate
def test_cli_asks_first_and_a_no_sends_nothing(client, monkeypatch):
    monkeypatch.setattr(transfer_module.ui, "is_interactive", lambda: True)
    responses.post(URL, json=OK)

    declined = _run("friend@example.com", "25", input="n\n")
    assert declined.exit_code == 0, declined.output
    assert "Transfer $25.00 to friend@example.com?" in declined.output
    assert len(responses.calls) == 0

    accepted = _run("friend@example.com", "25", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "Sent $25.00 to friend@example.com" in accepted.output
    assert len(responses.calls) == 1


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_cli_without_a_terminal_or_yes_fails_before_sending(client, extra):
    with responses.RequestsMock() as mocked:  # any request would fail the test
        result = _run("friend@example.com", "25", *extra)
    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "confirmation_required" in result.output or "Confirmation required" in result.output
    assert len(mocked.calls) == 0


@responses.activate
def test_cli_yes_json_prints_the_result(client):
    responses.post(URL, json=OK)

    result = _run("friend@example.com", "25", "--note", "hi", "--yes", "--json")

    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["transfer_id"] == "t-1" and out["sender_balance"] == 75.0 and out["amount"] == 25.0
    assert json.loads(responses.calls[0].request.body)["description"] == "hi"


@pytest.mark.parametrize(
    "status, code",
    [(402, "insufficient_balance"), (400, "self_transfer"), (409, "recipient_unavailable")],
)
@responses.activate
def test_cli_api_refusals_exit_non_zero_with_the_api_code(client, status, code):
    responses.post(URL, json=_error(code, "refused"), status=status)

    result = _run("friend@example.com", "25", "--yes", "--json")

    assert result.exit_code == EXIT_API_ERROR, result.output
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == code


@responses.activate
def test_cli_key_without_billing_scope_gets_the_billing_key_hint(client):
    responses.post(URL, json=_error("forbidden", "API key 'agent' does not have the 'billing' scope"), status=403)

    result = _run("friend@example.com", "25", "--yes", "--json")

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "missing_scope"
    assert "lium keys create payer --scope billing" in error["hint"]


@responses.activate
def test_cli_passes_the_idempotency_key_through(client):
    responses.post(URL, json=OK)

    result = _run("friend@example.com", "25", "--idempotency-key", "k-7", "--yes")

    assert result.exit_code == 0, result.output
    assert responses.calls[0].request.headers["Idempotency-Key"] == "k-7"
    assert "k-7" in result.output


@pytest.mark.parametrize("answer", [requests.Timeout("read timed out"), 502])
@responses.activate
def test_cli_lost_answer_is_outcome_unknown_with_the_key(client, answer):
    if isinstance(answer, int):
        responses.post(URL, json=_error("server_error", "bad gateway"), status=answer)
    else:
        responses.post(URL, body=answer)

    text = _run("friend@example.com", "25", "--idempotency-key", "k-9", "--yes")
    assert text.exit_code == EXIT_PERMISSION_DENIED, text.output
    assert "may or may not have happened" in text.output and "k-9" in text.output
    assert len(responses.calls) == 1  # posted once, never retried blindly

    as_json = _run("friend@example.com", "25", "--yes", "--json")
    assert as_json.exit_code == EXIT_PERMISSION_DENIED
    error = json.loads(as_json.stderr.strip().splitlines()[-1])["error"]
    assert error["code"] == "transfer_outcome_unknown"
    sent = responses.calls[-1].request.headers["Idempotency-Key"]
    assert sent in error["hint"]
    assert json.loads(as_json.stderr.strip().splitlines()[-1])["data"]["idempotency_key"] == sent
