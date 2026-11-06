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
from lium.sdk import Config, Lium, LiumChargeOutcomeUnknownError, LiumError, LiumTransferInterruptedError

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


@responses.activate
def test_cli_lost_answer_advises_no_rerun_only_billing_and_support(client):
    # until every backend keeps the key pending on a raised 500, a same-key re-run can move the credit again
    responses.post(URL, body=requests.Timeout("read timed out"))

    result = _run("friend@example.com", "25", "--note", "rent split", "--idempotency-key", "k-3", "--yes", "--json")
    hint = json.loads(result.stderr.strip().splitlines()[-1])["error"]["hint"]
    assert "lium transfer" not in hint and "once" not in hint and "twice" not in hint
    assert "Do not run it again" in hint and "Billing page" in hint and "contact support" in hint and "k-3" in hint


@pytest.mark.parametrize("answer", [requests.Timeout("read timed out"), 502, 409])
@responses.activate
def test_sdk_lost_or_unrecorded_answer_is_outcome_unknown_with_the_key(client, answer):
    # 409 idempotency_outcome_unknown: a same-key repeat of a transfer whose answer the server could not record
    if answer == 409:
        responses.post(URL, status=409, json=_error("idempotency_outcome_unknown", "Answer not recorded",
                                                    hint="Check `lium ps` before sending with a new key"))
    elif isinstance(answer, int):
        responses.post(URL, json=_error("server_error", "bad gateway"), status=answer)
    else:
        responses.post(URL, body=answer)

    with pytest.raises(LiumChargeOutcomeUnknownError) as raised:
        client.transfer_credits("friend@example.com", 25, idempotency_key="k-9")

    assert raised.value.code == "transfer_outcome_unknown"
    assert raised.value.idempotency_key == "k-9"
    assert "lium ps" not in (raised.value.hint or "") and "contact support" in raised.value.hint
    assert "moves the credit once" not in str(raised.value)


@responses.activate
def test_cli_unrecorded_answer_keeps_exit_6_and_the_key(client):
    responses.post(URL, status=409, json=_error("idempotency_outcome_unknown", "Answer not recorded",
                                                hint="Check `lium ps` before sending with a new key"))

    result = _run("friend@example.com", "25", "--idempotency-key", "k-9", "--yes", "--json")

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    last = json.loads(result.stderr.strip().splitlines()[-1])
    assert last["error"]["code"] == "transfer_outcome_unknown"
    assert last["data"]["idempotency_key"] == "k-9"
    assert "Billing page" in last["error"]["hint"] and "lium balance" not in last["error"]["hint"]


@pytest.mark.parametrize("typed, cents", [("0.015", "0.02"), ("1.005", "1.01"), ("2.675", "2.68"), ("25", "25.00")])
@responses.activate
def test_cli_confirms_sends_and_reports_the_amount_the_api_debits(client, monkeypatch, typed, cents):
    monkeypatch.setattr(transfer_module.ui, "is_interactive", lambda: True)
    responses.post(URL, json=OK)

    result = _run("friend@example.com", typed, input="y\n")
    as_json = _run("friend@example.com", typed, "--yes", "--json")

    assert result.exit_code == 0 and as_json.exit_code == 0, result.output + as_json.output
    assert f"Transfer ${cents} to friend@example.com?" in result.output
    assert f"Sent ${cents} to friend@example.com" in result.output
    assert json.loads(responses.calls[0].request.body)["amount"] == float(cents)
    assert json.loads(as_json.stdout)["amount"] == float(cents)


@pytest.mark.parametrize("typed", ["0.004", "1e400"])
def test_cli_refuses_an_amount_below_a_cent_or_too_large_before_sending(client, typed):
    with responses.RequestsMock() as mocked:
        result = _run("friend@example.com", typed, "--yes", "--json")
    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert len(mocked.calls) == 0


@pytest.mark.parametrize("code", ["idempotency_outcome_unknown", "idempotency_in_progress"])
@responses.activate
def test_an_unresolved_same_key_answer_keeps_the_key_and_sends_no_second_post(client, code):
    # the first call timed out (it may commit); the same-key repeat finds it unresolved: pending, or still running
    responses.post(URL, body=requests.Timeout("read timed out"))
    responses.post(URL, status=409, json=_error(code, "unresolved", hint="retry in a moment"),
                   headers={"Retry-After": "1"})

    first = _run("friend@example.com", "25", "--idempotency-key", "k-5", "--yes", "--json")
    again = _run("friend@example.com", "25", "--idempotency-key", "k-5", "--yes", "--json")

    for result in (first, again):
        assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
        last = json.loads(result.stderr.strip().splitlines()[-1])
        assert last["error"]["code"] == "transfer_outcome_unknown"
        assert last["data"]["idempotency_key"] == "k-5"
        assert "retry" not in last["error"]["hint"].lower() and "Do not run it again" in last["error"]["hint"]
    assert len(responses.calls) == 2  # one POST per run, never a retry of its own


@responses.activate
def test_ctrl_c_while_waiting_is_outcome_unknown_with_the_key_and_one_post(client):
    def committed_then_interrupted(_request):
        raise KeyboardInterrupt  # the server committed; the renter pressed Ctrl-C before the answer came

    responses.add_callback(responses.POST, URL, callback=committed_then_interrupted)

    result = _run("friend@example.com", "25", "--idempotency-key", "k-6", "--yes", "--json")

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    last = json.loads(result.stderr.strip().splitlines()[-1])
    assert last["error"]["code"] == "transfer_outcome_unknown"
    assert last["data"]["idempotency_key"] == "k-6"
    assert "lium transfer" not in last["error"]["hint"]
    assert len(responses.calls) == 1


@pytest.mark.parametrize("recipient", ["missing@example.com", "3f1c0000-0000-0000-0000-00000000dead"])
@pytest.mark.parametrize(
    "status, code",
    [(404, "not_found"), (409, "recipient_unavailable")],
    ids=["missing", "deleted-or-blocked"],
)
@responses.activate
def test_cli_recipient_refusals_pass_the_api_code_through(client, recipient, status, code):
    responses.post(URL, status=status, json=_error(code, "refused"))

    result = _run(recipient, "25", "--yes", "--json")

    assert result.exit_code != 0, result.output
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == code
    assert len(responses.calls) == 1


@pytest.mark.parametrize("status, code", [(400, "self_transfer"), (404, "not_found"), (409, "recipient_unavailable")])
@responses.activate
def test_cli_refusals_without_a_server_hint_get_a_transfer_hint(client, status, code):
    # live 0.39.0 run: self_transfer said "Retry", not_found pointed at nodes and pods
    responses.post(URL, status=status, json=_error(code, "refused"))

    result = _run("friend@example.com", "25", "--yes", "--json")

    error = json.loads(result.stderr.strip().splitlines()[-1])["error"]
    assert result.exit_code == EXIT_API_ERROR, result.output
    assert (error["code"], error["hint"]) == (code, transfer_module.REFUSAL_HINTS[code])


@responses.activate
def test_cli_a_server_hint_on_a_refusal_wins(client):
    responses.post(URL, status=404, json=_error("not_found", "refused", hint="Ask the recipient to sign up first."))

    result = _run("friend@example.com", "25", "--yes", "--json")

    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["hint"] == "Ask the recipient to sign up first."


@pytest.mark.parametrize("stored", ["retained", "pending"])
@responses.activate
def test_sdk_same_key_repeat_contract(client, stored):
    # retained: the platform replays the recorded answer; pending: it refuses, and the SDK keeps it unresolved.
    # An expired record (after 24 h) is forgotten server-side and the repeat transfers again: the help says so.
    if stored == "retained":
        responses.post(URL, json=OK, headers={"Idempotent-Replayed": "true"})
        assert client.transfer_credits("friend@example.com", 25, idempotency_key="k-8")["transfer_id"] == "t-1"
    else:
        responses.post(URL, status=409, json=_error("idempotency_outcome_unknown", "pending"))
        with pytest.raises(LiumChargeOutcomeUnknownError) as raised:
            client.transfer_credits("friend@example.com", 25, idempotency_key="k-8")
        assert raised.value.idempotency_key == "k-8"


def test_the_replay_promise_is_limited_to_24_hours_on_both_surfaces():
    help_text = CliRunner().invoke(cli, ["transfer", "--help"]).output
    assert "Within 24 h" in help_text and "transfers again" in help_text
    doc = Lium.transfer_credits.__doc__
    assert "24 h" in doc and "transfers again" in doc


@pytest.mark.parametrize(
    "stored, body, answer, unresolved",
    [
        ("pending", "same", (409, "idempotency_outcome_unknown"), True),
        ("pending", "changed", (422, "idempotency_key_reused"), True),
        ("completed", "same", (200, None), False),
        ("completed", "changed", (422, "idempotency_key_reused"), True),
        ("refused", "same", (402, "insufficient_balance"), False),  # the stored refusal replayed
        ("refused", "changed", (200, None), False),  # a corrected retry runs
    ],
)
@responses.activate
def test_sdk_same_key_answers_by_stored_record_and_body(client, stored, body, answer, unresolved):
    status, code = answer
    if code is None:
        responses.post(URL, json=OK)
    else:
        responses.post(URL, status=status, json=_error(code, "answer", hint="send it again with a new key"))
    note = "changed" if body == "changed" else None

    if unresolved:
        with pytest.raises(LiumChargeOutcomeUnknownError) as raised:
            client.transfer_credits("friend@example.com", 25, description=note, idempotency_key="k-4")
        assert raised.value.code == "transfer_outcome_unknown" and raised.value.idempotency_key == "k-4"
        assert "new key" not in str(raised.value) and "do not send it again" in raised.value.hint.lower()
    elif code is None:
        assert client.transfer_credits("friend@example.com", 25, description=note, idempotency_key="k-4")["transfer_id"]
    else:
        with pytest.raises(LiumError) as raised:
            client.transfer_credits("friend@example.com", 25, description=note, idempotency_key="k-4")
        assert not isinstance(raised.value, LiumChargeOutcomeUnknownError)
        assert raised.value.code == code  # a definite refusal stays recoverable
    assert len(responses.calls) == 1


@responses.activate
def test_sdk_ctrl_c_after_the_post_stays_an_interrupt_and_keeps_the_generated_key(client):
    responses.add_callback(responses.POST, URL, callback=lambda _request: (_ for _ in ()).throw(KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt) as raised:
        try:
            client.transfer_credits("friend@example.com", 25)
        except Exception:  # a script's broad handler must not swallow the abort
            pytest.fail("Ctrl-C was turned into an Exception")

    sent = responses.calls[0].request.headers["Idempotency-Key"]
    assert isinstance(raised.value, LiumTransferInterruptedError)
    assert raised.value.code == "transfer_outcome_unknown"
    assert raised.value.idempotency_key == sent
    assert "do not send it again" in raised.value.hint.lower()
    assert len(responses.calls) == 1
