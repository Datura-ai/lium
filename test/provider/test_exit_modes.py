"""One exit map per output mode (``docs/exit-codes.md``), end to end against a local portal stub.

Text mode keeps the exit statuses scripts already rely on, for every error; under ``--json`` (or
``LIUM_OUTPUT=json``) every error exits by the unified map, whatever its origin. A refusal the portal
codes is ``portal.<code>`` in snake_case, with the UPPER_CASE code it replaces kept as ``legacy_code``.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from ._portal_stub import PortalStub, closed_port_url

TOKEN = "lpk_stub"
NODE = "7c1f0e2a-0000-4000-8000-000000000001"
ADD = ("node", "add", "--gpu-type", "H100", "--ip", "203.0.113.7", "--price", "2.5", "--yes")


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    stub = PortalStub()
    yield stub
    stub.close()


def run(url: str, *args: str, env: dict | None = None):
    base = {"LIUM_PROVIDER_TOKEN": TOKEN, "LIUM_PROVIDER_ACK": "", "LIUM_OUTPUT": ""}
    return CliRunner().invoke(provider_command, ["--portal-url", url, *args], env={**base, **(env or {})})


def refusal(code: str, message: str = "refused") -> dict:
    return {"detail": {"code": code, "message": message}}


# (route, command, portal status, portal code, text exit, json exit, json code, legacy code)
CODED = [
    (("DELETE", f"/executors/{NODE}"), ("node", "rm", NODE, "--yes"), 400, "NODE_RENTED",
     1, 3, "portal.node_rented", "PORTAL_REQUEST_REJECTED"),
    (("POST", "/executors"), ADD, 400, "NODE_ALREADY_ON_YOUR_ACCOUNT",
     1, 3, "portal.node_already_on_your_account", "PORTAL_REQUEST_REJECTED"),
    (("POST", "/executors"), ADD, 409, "NODE_REGISTERED_ELSEWHERE",
     1, 3, "portal.node_registered_elsewhere", "PORTAL_REQUEST_REJECTED"),
    (("POST", "/executors"), ADD, 400, "PROVIDER_EMAIL_REQUIRED",
     1, 3, "portal.provider_email_required", "PORTAL_REQUEST_REJECTED"),
    (("DELETE", f"/executors/{NODE}"), ("node", "rm", NODE, "--yes"), 404, "EXECUTOR_NOT_FOUND",
     3, 5, "portal.executor_not_found", "PORTAL_NOT_FOUND"),
    (("DELETE", f"/executors/{NODE}"), ("node", "rm", NODE, "--yes"), 403, "OWNERSHIP_MISMATCH",
     2, 6, "portal.ownership_mismatch", "PORTAL_FORBIDDEN"),
    (("GET", "/executors/listing"), ("node", "listing"), 429, "TOO_MANY_REQUESTS",
     3, 7, "portal.too_many_requests", "PORTAL_RATE_LIMIT"),
]
IDS = [c[3] for c in CODED]
REJECTED_HINT = "The portal refused this request (see message). Fix the input; retrying the same call will not help."
# main's text-mode stderr for each refusal, byte for byte (the portal's code only shows inside main's message)
MAIN_TEXT = {
    "NODE_RENTED": ("[PORTAL_REQUEST_REJECTED] portal rejected the request (400): code: NODE_RENTED; message: refused",
                    REJECTED_HINT),
    "NODE_ALREADY_ON_YOUR_ACCOUNT": ("[PORTAL_REQUEST_REJECTED] portal rejected the request (400): code: "
                                     "NODE_ALREADY_ON_YOUR_ACCOUNT; message: refused", REJECTED_HINT),
    "NODE_REGISTERED_ELSEWHERE": ("[PORTAL_REQUEST_REJECTED] portal rejected the request (409): code: "
                                  "NODE_REGISTERED_ELSEWHERE; message: refused", REJECTED_HINT),
    "PROVIDER_EMAIL_REQUIRED": ("[PORTAL_REQUEST_REJECTED] portal rejected the request (400): code: "
                                "PROVIDER_EMAIL_REQUIRED; message: refused", REJECTED_HINT),
    "EXECUTOR_NOT_FOUND": ("[PORTAL_NOT_FOUND] portal returned 404",
                           "The portal returned 404 for that resource (wrong UUID or already removed)."),
    "OWNERSHIP_MISMATCH": ("[PORTAL_FORBIDDEN] portal forbade the requested action",
                           "The portal accepted the token but refused the action for this hotkey (e.g. machine-request "
                           "detail needs a validator-verified node)."),
    "TOO_MANY_REQUESTS": ("[PORTAL_RATE_LIMIT] portal rate limit", "Backing off; retry shortly."),
}


@pytest.mark.parametrize("route, args, status, portal_code, text_exit, json_exit, code, legacy", CODED, ids=IDS)
def test_a_coded_refusal_keeps_its_old_exit_and_label_in_text_mode(portal, route, args, status, portal_code, text_exit,
                                                         json_exit, code, legacy) -> None:
    portal.route(*route, refusal(portal_code), status=status)
    result = run(portal.url, *args)
    assert result.exit_code == text_exit, result.output
    line, hint = MAIN_TEXT[portal_code]
    assert result.stderr == f"{line}\n  hint: {hint}\n" and result.stdout == ""


def test_text_debug_shows_mains_context_without_the_new_keys(portal) -> None:
    body = refusal("NODE_RENTED")
    portal.route("DELETE", f"/executors/{NODE}", body, status=400)
    result = run(portal.url, "--debug", "node", "rm", NODE, "--yes")
    context = {"url": f"{portal.url}/executors/{NODE}", "method": "DELETE", "status": 400, "body": body}
    line, hint = MAIN_TEXT["NODE_RENTED"]
    assert result.stderr == f"{line}\n  hint: {hint}\n  context: {context}\n"
    error = json.loads(run(portal.url, "--json", "--debug", "node", "rm", NODE, "--yes").stdout)["error"]
    assert error["data"]["portal_code"] == "NODE_RENTED"


@pytest.mark.parametrize("route, args, status, portal_code, text_exit, json_exit, code, legacy", CODED, ids=IDS)
def test_a_coded_refusal_exits_by_the_unified_map_under_json(portal, route, args, status, portal_code, text_exit,
                                                            json_exit, code, legacy) -> None:
    portal.route(*route, refusal(portal_code), status=status)
    for result in (run(portal.url, "--json", *args), run(portal.url, *args, env={"LIUM_OUTPUT": "json"})):
        assert result.exit_code == json_exit, result.output
        error = json.loads(result.stdout)["error"]
        assert (error["code"], error["legacy_code"], error["exit_code"]) == (code, legacy, json_exit)
        assert error["data"]["portal_code"] == portal_code


# Errors that were UPPER_CASE before the unified map: text keeps the old status, --json the unified one.
UNCODED = [
    ((401, {"detail": "Invalid token"}), "PORTAL_AUTH_INVALID", "auth.invalid", 2, 6),
    ((403, {"detail": "Forbidden"}), "PORTAL_FORBIDDEN", "auth.forbidden", 2, 6),
    ((404, {"detail": "Not Found"}), "PORTAL_NOT_FOUND", "portal.not_found", 3, 5),
    ((429, {"detail": "Slow down"}), "PORTAL_RATE_LIMIT", "portal.rate_limited", 3, 7),
    ((400, {"detail": "Unsupported gpu type."}), "PORTAL_REQUEST_REJECTED", "portal.request_rejected", 1, 3),
    ((500, {"detail": "boom"}), "PORTAL_SERVER_ERROR", "portal.server_error", 3, 3),
]


@pytest.mark.parametrize("answer, legacy, code, text_exit, json_exit", UNCODED, ids=[u[1] for u in UNCODED])
def test_an_uncoded_error_exits_old_in_text_and_unified_under_json(portal, answer, legacy, code, text_exit,
                                                                  json_exit) -> None:
    status, body = answer
    portal.route("GET", "/executors/listing", body, status=status)
    assert run(portal.url, "node", "listing").exit_code == text_exit
    result = run(portal.url, "--json", "node", "listing")
    assert result.exit_code == json_exit, result.output
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["legacy_code"], error["exit_code"]) == (code, legacy, json_exit)


def test_no_sign_in_exits_1_in_text_and_6_under_json(portal) -> None:
    env = {"LIUM_PROVIDER_TOKEN": ""}
    assert run(portal.url, "node", "listing", env=env).exit_code == 1
    result = run(portal.url, "--json", "node", "listing", env=env)
    assert result.exit_code == 6
    error = json.loads(result.stdout)["error"]
    assert (error["code"], error["legacy_code"]) == ("auth.not_signed_in", "ARG_INVALID")


def test_the_missing_hotkey_hint_is_the_old_one_in_text_and_names_the_token_under_json(portal) -> None:
    env = {"LIUM_PROVIDER_TOKEN": ""}
    text = run(portal.url, "node", "listing", env=env)
    assert "[ARG_INVALID] node commands require --hotkey" in text.stderr and "LIUM_PROVIDER_TOKEN" not in text.stderr
    error = json.loads(run(portal.url, "--json", "node", "listing", env=env).stdout)["error"]
    assert "LIUM_PROVIDER_TOKEN" in error["hint"]


def test_an_unreachable_portal_reads_and_exits_as_before_in_text_and_4_under_json() -> None:
    url = closed_port_url()
    result = run(url, "node", "listing")
    assert result.exit_code == 3 and "[PORTAL_SERVER_ERROR] network error reaching portal: " in result.stderr
    assert "hint: Portal 5xx." in result.stderr and "net.unreachable" not in result.stderr
    debug = run(url, "--debug", "node", "listing").stderr
    assert debug.endswith(f"  context: {{'url': '{url}/executors/listing', 'method': 'GET'}}\n"), debug
    error = json.loads(run(url, "--json", "node", "listing").stdout)["error"]
    assert (error["code"], error["legacy_code"], error["exit_code"]) == ("net.unreachable", "PORTAL_SERVER_ERROR", 4)

