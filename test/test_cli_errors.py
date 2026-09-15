"""One error shape, one exit-code table, and a next step on every failure.

A program driving the CLI needs three things from a failure: a stable code to
branch on, the exit status, and what to do about it. The envelope carries all
three, `LIUM_OUTPUT=json` turns it on without a per-command flag, and the
documented exit-code table is checked against the constants the code uses.
"""

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from lium.cli import utils
from lium.cli.cli import cli
from lium.cli.ps import command as ps_module
from lium.cli.utils import (
    EXIT_API_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_PERMISSION_DENIED,
    EXIT_POD_NOT_FOUND,
    EXIT_SSH_ERROR,
    CliFailure,
    default_hint,
    error_envelope,
)
from lium.sdk import client as utils_client_module
from lium.sdk import (
    LiumAuthError,
    LiumError,
    LiumHostKeyError,
    LiumInsufficientBalanceError,
    LiumNotFoundError,
    LiumPermissionError,
    LiumRateLimitError,
    LiumServerError,
    LiumSessionError,
)

DOC = Path(__file__).resolve().parent.parent / "docs" / "exit-codes.md"


@pytest.fixture(autouse=True)
def _clean_output_env(monkeypatch):
    monkeypatch.delenv(utils.OUTPUT_ENV, raising=False)


def _run_ps_raising(monkeypatch, error: Exception, args=(), env=None):
    class _RaisingLium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            raise error

    monkeypatch.setattr(ps_module, "Lium", _RaisingLium)
    # without a ~/.lium/config.ini `lium ps` runs the interactive key setup before
    # reaching the client; stub it so the test sees the injected error on a fresh runner
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["ps", *args], env=env)


# --- the envelope -----------------------------------------------------------------

def test_envelope_has_code_message_hint_and_exit_code():
    envelope = error_envelope("pod_not_found", "No pods matching: x", EXIT_POD_NOT_FOUND)

    assert envelope["ok"] is False
    assert set(envelope["error"]) == {"code", "message", "hint", "exit_code"}
    assert envelope["error"]["exit_code"] == EXIT_POD_NOT_FOUND
    assert "lium ps" in envelope["error"]["hint"]


def test_a_failure_may_bring_its_own_hint():
    failure = CliFailure("invalid_arguments", "bad", EXIT_CONFIGURATION_ERROR, hint="pass --ttl 1h")

    assert failure.hint == "pass --ttl 1h"
    assert error_envelope(failure.code, failure.message, failure.exit_code, hint=failure.hint)["error"]["hint"] == "pass --ttl 1h"


def test_a_failure_without_a_hint_gets_one_for_its_code():
    assert CliFailure("confirmation_required", "x", EXIT_CONFIGURATION_ERROR).hint == default_hint("confirmation_required")
    assert "--yes" in default_hint("confirmation_required")


@pytest.mark.parametrize("exit_code", [
    EXIT_GENERAL_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_API_ERROR,
    EXIT_SSH_ERROR, EXIT_POD_NOT_FOUND, EXIT_PERMISSION_DENIED,
])
def test_an_unknown_code_still_gets_a_hint_from_its_exit_code(exit_code):
    """No error leaves without a next step, whatever a command named it."""
    assert default_hint("some_new_command_specific_code", exit_code)


def test_envelope_goes_to_stderr_and_stdout_stays_empty(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumServerError("Server error: 502"), ["--format", "json"])

    assert result.exit_code == EXIT_API_ERROR
    assert result.stdout == ""
    payload = json.loads(result.stderr)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "server_error"
    assert payload["error"]["exit_code"] == EXIT_API_ERROR
    assert payload["error"]["hint"]


def test_json_message_carries_no_human_prefix(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumServerError("Server error: 502"), ["--format", "json"])

    assert json.loads(result.stderr)["error"]["message"] == "Server error: 502"


# --- LIUM_OUTPUT=json -------------------------------------------------------------

def test_output_env_turns_failures_into_json_without_a_flag(monkeypatch):
    result = _run_ps_raising(
        monkeypatch, LiumServerError("Server error: 502"), env={utils.OUTPUT_ENV: "json"}
    )

    assert result.exit_code == EXIT_API_ERROR
    assert json.loads(result.stderr)["error"]["code"] == "server_error"


def test_output_env_with_another_value_keeps_text(monkeypatch):
    result = _run_ps_raising(
        monkeypatch, LiumServerError("Server error: 502"), env={utils.OUTPUT_ENV: "table"}
    )

    assert result.exit_code == EXIT_API_ERROR
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.output)


# --- classification of SDK errors ---------------------------------------------------

@pytest.mark.parametrize("error, code, exit_code", [
    (LiumAuthError("Invalid API key"), "invalid_api_key", EXIT_API_ERROR),
    # a LiumAuthError too, but the hint must name the login, not an API key (DAH-3033)
    (LiumSessionError("This needs a browser session, not an API key"), "session_required", EXIT_API_ERROR),
    (LiumPermissionError("User is not verified"), "permission_denied", EXIT_PERMISSION_DENIED),
    (LiumInsufficientBalanceError("Insufficient balance", required=4.0, available=1.0),
     "insufficient_balance", EXIT_PERMISSION_DENIED),
    (LiumNotFoundError("Resource not found: /x"), "not_found", EXIT_API_ERROR),
    (LiumRateLimitError("Rate limit exceeded"), "rate_limited", EXIT_API_ERROR),
    (LiumServerError("Server error: 503"), "server_error", EXIT_API_ERROR),
    (LiumError("API error 418: teapot"), "lium_error", EXIT_API_ERROR),
    # main's host-key pinning (DAH-2904): a changed key is an ssh failure to look at, never a retry
    (LiumHostKeyError("Host key for pod x (203.0.113.5:2222) changed"), "ssh_host_key_changed", EXIT_SSH_ERROR),
    (ValueError("No API key found. Set LIUM_API_KEY"), "no_api_key", EXIT_CONFIGURATION_ERROR),
    (ValueError("bad value"), "value_error", EXIT_CONFIGURATION_ERROR),
    (RuntimeError("boom"), "unexpected_error", EXIT_GENERAL_ERROR),
])
def test_every_sdk_error_maps_to_a_code_and_exit_status(monkeypatch, error, code, exit_code):
    result = _run_ps_raising(monkeypatch, error, ["--format", "json"])

    assert result.exit_code == exit_code
    assert json.loads(result.stderr)["error"]["code"] == code


@pytest.mark.parametrize("detail, required, available", [
    ("Insufficient balance. This node costs $2.00/hour, so renting it requires at least $0.50 "
     "(15 minutes of runtime). Your balance is $0.12.", 0.5, 0.12),
    ("Insufficient balance", None, None),
])
def test_a_403_for_lack_of_funds_is_an_insufficient_balance_error(detail, required, available):
    from lium.sdk.client import permission_error

    error = permission_error(detail)

    assert isinstance(error, LiumInsufficientBalanceError)
    assert (error.required, error.available) == (required, available)
    assert str(error) == f"Permission denied: {detail}"


def test_any_other_403_stays_a_plain_permission_error():
    from lium.sdk.client import permission_error

    error = permission_error("User is not verified")

    assert type(error) is LiumPermissionError


def test_the_structured_error_code_decides_over_the_message_text():
    """lium-platform#210's ``error.code`` is the contract; the wording is not."""
    from lium.sdk.client import permission_error

    by_code = permission_error("Not enough funds for this node. Your balance is $0.12.", "insufficient_balance")
    assert isinstance(by_code, LiumInsufficientBalanceError)
    assert (by_code.required, by_code.available) == (None, 0.12)

    other_code = permission_error("Insufficient balance verification pending", "account_not_verified")
    assert type(other_code) is LiumPermissionError


def test_a_structured_403_response_is_classified_by_its_code(monkeypatch):
    """The platform's structured body: ``error.code`` is read, the message keeps the amounts."""
    from types import SimpleNamespace

    from lium.sdk import Config, Lium

    body = {
        "success": False,
        "error": {"code": "insufficient_balance", "message": "Insufficient balance. This node costs $2.00/hour, "
                  "so renting it requires at least $0.50 (15 minutes of runtime). Your balance is $0.12.",
                  "hint": "Top up", "request_id": "req-1"},
        "message": "Insufficient balance. This node costs $2.00/hour, so renting it requires at least $0.50 "
                   "(15 minutes of runtime). Your balance is $0.12.",
        "status_code": 403,
    }
    response = SimpleNamespace(ok=False, status_code=403, text=json.dumps(body), json=lambda: body)
    monkeypatch.setattr(utils_client_module.requests, "request", lambda *a, **k: response)

    with pytest.raises(LiumInsufficientBalanceError) as caught:
        Lium(Config(api_key="k"))._request("GET", "/pods")

    assert (caught.value.required, caught.value.available) == (0.5, 0.12)


def test_a_response_without_a_structured_code_falls_back_to_the_text(monkeypatch):
    from types import SimpleNamespace

    from lium.sdk import Config, Lium

    body = {"detail": "User is not verified", "error": "Forbidden"}
    response = SimpleNamespace(ok=False, status_code=403, text=json.dumps(body), json=lambda: body)
    monkeypatch.setattr(utils_client_module.requests, "request", lambda *a, **k: response)

    with pytest.raises(LiumPermissionError) as caught:
        Lium(Config(api_key="k"))._request("GET", "/pods")

    assert type(caught.value) is LiumPermissionError
    assert "User is not verified" in str(caught.value)


def test_a_403_response_is_classified_through_request(monkeypatch):
    """End to end: the API's 403 body reaches the CLI as insufficient_balance."""
    from types import SimpleNamespace

    from lium.sdk import Config, Lium

    body = {"detail": "Insufficient balance. This node costs $2.00/hour, so renting it requires at least $0.50 "
                      "(15 minutes of runtime). Your balance is $0.12."}
    response = SimpleNamespace(ok=False, status_code=403, text=json.dumps(body), json=lambda: body)
    monkeypatch.setattr(utils_client_module.requests, "request", lambda *a, **k: response)

    with pytest.raises(LiumInsufficientBalanceError) as caught:
        Lium(Config(api_key="k"))._request("GET", "/pods")

    assert (caught.value.required, caught.value.available) == (0.5, 0.12)


def test_insufficient_balance_is_still_a_permission_error():
    """Callers that catch the parent must keep working."""
    error = LiumInsufficientBalanceError("Insufficient balance", required=4.0, available=1.0)

    assert isinstance(error, LiumPermissionError)
    assert (error.required, error.available) == (4.0, 1.0)


# --- human rendering ---------------------------------------------------------------

def test_text_mode_prints_the_hint_under_the_error(monkeypatch):
    result = _run_ps_raising(monkeypatch, LiumPermissionError("User is not verified"))

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert "User is not verified" in result.output
    assert "lium balance" in result.output


def test_text_mode_does_not_repeat_a_hint_the_message_already_carries(monkeypatch):
    class _Lium:
        def __init__(self, *a, **k):
            pass

        def ps(self):
            raise CliFailure("confirmation_required", "Confirmation required (re-run with --yes)",
                             EXIT_CONFIGURATION_ERROR, hint="re-run with --yes")

    monkeypatch.setattr(ps_module, "Lium", _Lium)
    monkeypatch.setattr(ps_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["ps"])

    assert result.output.lower().count("re-run with --yes") == 1


# --- LIUM_DEBUG ---------------------------------------------------------------------

def test_debug_prints_the_traceback_on_stderr(monkeypatch):
    result = _run_ps_raising(monkeypatch, RuntimeError("boom"), env={"LIUM_DEBUG": "1"})

    assert result.exit_code == EXIT_GENERAL_ERROR
    assert "Traceback (most recent call last)" in result.output
    assert "RuntimeError: boom" in result.output


def test_debug_keeps_the_json_envelope_parseable(monkeypatch):
    result = _run_ps_raising(
        monkeypatch, LiumServerError("Server error: 502"), ["--format", "json"], env={"LIUM_DEBUG": "1"}
    )

    assert result.stdout == ""
    assert "Traceback" in result.stderr
    assert json.loads(result.stderr.strip().splitlines()[-1])["error"]["code"] == "server_error"


def test_without_debug_there_is_no_traceback(monkeypatch):
    result = _run_ps_raising(monkeypatch, RuntimeError("boom"))

    assert "Traceback" not in result.output


# --- the documentation mirrors the code ------------------------------------------------

def test_exit_code_doc_lists_every_constant_with_its_value():
    doc = DOC.read_text()
    for name, value in vars(utils).items():
        if not name.startswith("EXIT_"):
            continue
        assert re.search(rf"^\|\s*{value}\s*\|\s*`{name}`", doc, re.M), f"{name}={value} missing from {DOC.name}"


def test_exit_code_doc_lists_every_shared_error_code():
    doc = DOC.read_text()
    for code in utils._HINTS_BY_CODE:
        assert f"`{code}`" in doc, f"{code} missing from {DOC.name}"
