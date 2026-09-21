"""`lium keys create --scope/--daily-budget/--max-budget/--pod-visibility`, `lium keys show`, `lium keys scopes`,
`lium ps --key`, `lium billing history --key`, and the SDK behind them (`Lium.api_keys`, `Lium.ps(api_key_id=…)`,
`Lium.billing_statement`, `LiumBudgetExceededError`, `LiumScopeError`), `lium keys budget` — lium-platform#630, not released.

HTTP is answered from test/fixtures/api_keys (and the workspaces fixtures for `/users/me`, `/workspaces`, `/pods`) with
`responses`, so the real SDK request path runs: what is asserted is the request the CLI sends and what it prints.
"""

import json
import re
from pathlib import Path

import pytest
import responses

from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED, _classify_sdk_error
from lium.sdk import Config, Lium, LiumBudgetExceededError, LiumPermissionError, LiumScopeError
from lium.sdk.api_keys import BILLING_ALONE, DEFAULT_SCOPES
from lium.sdk.client import permission_error
from lium.sdk.exceptions import LiumNotFoundError

from test_workspaces_cli import API, RESEARCH, home, me, run  # noqa: F401 - the `home` fixture, as test_up_eta imports its helpers

FIXTURES = Path(__file__).parent / "fixtures" / "api_keys"
WORKSPACE_FIXTURES = Path(__file__).parent / "fixtures" / "workspaces"
AGENT_KEY = "5b4a3c2d-1e0f-4a9b-8c7d-6e5f4a3b2c1d"
OPS_KEY = "c0ffee00-1111-4222-8333-444455556666"
SESSION = "eyJ.fixture.session"


@pytest.fixture(autouse=True)
def wide_terminal(monkeypatch):
    """Rich folds table cells at CliRunner's 80 columns; the assertions below read whole cells. Pinned on the
    console itself, as test_ls_width does: FORCE_COLOR + TERM=dumb in a shell would ignore a COLUMNS export."""
    from lium.cli import utils

    monkeypatch.setattr(utils.console, "_width", 240)
    monkeypatch.setattr(utils.console, "_height", 50)


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def ws_fixture(name):
    return json.loads((WORKSPACE_FIXTURES / f"{name}.json").read_text())


def scope_rows() -> list[dict]:
    return fixture("scopes")["scopes"]


def session(monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", SESSION)
    me()
    responses.add(responses.GET, f"{API}/workspaces", json=ws_fixture("workspaces_session"))


def calls_to(path_suffix: str, method: str | None = None):
    return [
        c for c in responses.calls
        if c.request.url.split("?")[0].endswith(path_suffix) and (method is None or c.request.method == method)
    ]


def query_of(call) -> dict:
    from urllib.parse import parse_qs, urlparse

    return {k: v[0] for k, v in parse_qs(urlparse(call.request.url).query).items()}


def created(name="ci", **fields):
    """A `POST /keys` answer from a lium-platform#630 server: the DAH-2944 row plus the P235 fields, no budget."""
    return {**ws_fixture("key_created"), "name": name, "daily_budget_usd": None, "monthly_budget_usd": None,
            "max_budget_usd": None, "spent_today_usd": 0.0, "spent_month_usd": 0.0, "spent_total_usd": 0.0,
            "pod_visibility": "own", "pods_count": 0, **fields}


def no_refusals_route():
    """A server before the refusal ledger: `GET /keys/{id}/refusals` is no route (404)."""
    responses.add(responses.GET, re.compile(rf"{re.escape(API)}/keys/[^/]+/refusals"), status=404, json={"detail": "Not Found"})


def pods_rented_by_agent():
    """`GET /pods` rows: the workspaces fixture's pod, rented through agent-1 (`api_key_id` as P235 names it)."""
    rows = ws_fixture("pods_research")
    rows[0]["api_key_id"] = AGENT_KEY
    rows[0]["api_key_name"] = "agent-1"
    return rows


# ------------------------------------------------------------------------------------------------- keys create
@responses.activate
def test_create_without_scope_sends_read_rent_manage_and_never_billing(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.POST, f"{API}/keys", json=created("ci"))

    result = run("keys", "create", "ci")

    assert result.exit_code == 0, result.output
    body = json.loads(calls_to("/keys", "POST")[0].request.body)
    assert body["scopes"] == list(DEFAULT_SCOPES) and "billing" not in body["scopes"]
    assert body == {"name": "ci", "scopes": ["read", "rent", "manage"], "pod_visibility": "own"}
    assert not calls_to("/keys/scopes")  # no warning to print, so the scopes route is not read
    assert "Warning" not in result.output


@responses.activate
def test_create_on_a_server_before_budgets_says_the_budget_and_visibility_were_not_recorded(home, monkeypatch):
    """Today's server (dtos/api_key.py on main) ignores fields it does not know: the key is minted with no budget and
    sees the whole account. The row it answers has no `pod_visibility`; the CLI says so instead of printing a cap
    that does not exist. The key is still printed and exit is 0: it is real."""
    session(monkeypatch)
    responses.add(responses.POST, f"{API}/keys", json=ws_fixture("key_created"))  # the DAH-2944 row

    result = run("keys", "create", "ci", "--daily-budget", "20", "--max-budget", "200")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "sk_test_fixture_key_not_a_secret_0000000000" in text
    assert ("Warning: this server has no per-key budgets or pod visibility yet (lium-platform#630, not released): "
            "--daily-budget $20.00 and --max-budget $200.00 were not recorded — the key has no budget, and it sees every "
            "pod of the account") in text
    assert "Budget:" not in text  # nothing to show as set

    machine = run("keys", "create", "ci", "--json")
    assert machine.exit_code == 0 and json.loads(machine.stdout)["key"]
    assert "Warning: this server has no per-key budgets" in machine.stderr and "the key sees every pod" in machine.stderr


@responses.activate
def test_create_sends_the_named_scopes_and_budgets_as_numbers(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created_budget"))

    result = run(
        "keys", "create", "agent-1", "--scope", "read", "--scope", "rent", "--scope", "rent",
        "--daily-budget", "20", "--monthly-budget", "300", "--max-budget", "2000", "--pod-visibility", "own",
    )

    assert result.exit_code == 0, result.output
    body = json.loads(calls_to("/keys", "POST")[0].request.body)
    assert body["scopes"] == ["read", "rent"]  # the repeated --scope rent is sent once
    assert body["daily_budget_usd"] == 20.0 and isinstance(body["daily_budget_usd"], float)
    assert body["monthly_budget_usd"] == 300.0 and isinstance(body["monthly_budget_usd"], float)
    assert body["max_budget_usd"] == 2000.0 and isinstance(body["max_budget_usd"], float)
    assert body["pod_visibility"] == "own"
    assert "sk_test_fixture_key_not_a_secret_0000000000" in result.output
    text = " ".join(result.output.split())
    assert "$0.00/$20.00 today · $0.00/$300.00 month · $0.00/$200.00 total" in text  # the row as the server answered it


@responses.activate
def test_create_with_only_a_monthly_budget_sends_that_one_field(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.POST, f"{API}/keys", json=created("m", monthly_budget_usd=300.0))

    result = run("keys", "create", "m", "--monthly-budget", "300")

    assert result.exit_code == 0, result.output
    body = json.loads(calls_to("/keys", "POST")[0].request.body)
    assert body == {"name": "m", "scopes": ["read", "rent", "manage"], "pod_visibility": "own", "monthly_budget_usd": 300.0}
    assert "Budget: $0.00/$300.00 month" in " ".join(result.output.split())


@responses.activate
def test_create_with_billing_scope_prints_the_servers_warning_before_the_post(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.POST, f"{API}/keys", json=created("payer", scopes=["billing"], pod_visibility="account"))

    result = run("keys", "create", "payer", "--scope", "billing", "--pod-visibility", "account")

    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    billing = next(s for s in scope_rows() if s["scope"] == "billing")["description"]
    assert f"Warning: this key holds the 'billing' scope — {billing}" in text
    order = [c.request.url.split("?")[0].rsplit("/api", 1)[1] for c in responses.calls if "/keys" in c.request.url]
    assert order == ["/keys/scopes", "/keys"]  # the warning's words are read before anything is minted
    body = json.loads(calls_to("/keys", "POST")[0].request.body)
    assert body["scopes"] == ["billing"] and body["pod_visibility"] == "account"


@pytest.mark.parametrize("other", ["rent", "manage", "read"])
def test_create_refuses_billing_with_any_other_scope_before_any_request(home, other):
    """Conductor 12:11Z: `billing` is "and nothing else". The server answers 422 to billing + rent/manage
    (lium-platform#630, not released); the CLI says the same before the request, so no warning is printed
    for a key that is never minted and the scopes route is not read."""
    with responses.RequestsMock() as mocked:
        result = run("keys", "create", "payer", "--scope", "billing", "--scope", other, "--json")

        assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
        error = json.loads(result.stderr)["error"]
        assert error["code"] == "invalid_arguments"
        assert error["message"] == BILLING_ALONE
        assert "billing with read, rent or manage is refused" in error["message"]
        assert "Warning" not in result.stderr
        assert len(mocked.calls) == 0

    plain = run("keys", "create", "payer", "--scope", other, "--scope", "billing")
    assert plain.exit_code == EXIT_CONFIGURATION_ERROR and "stands alone" in " ".join(plain.output.split())


@responses.activate
def test_create_with_billing_scope_under_json_puts_the_warning_on_stderr(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.POST, f"{API}/keys", json=created("payer", scopes=["billing"]))

    result = run("keys", "create", "payer", "--scope", "billing", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["key"] == "sk_test_fixture_key_not_a_secret_0000000000"
    assert "Warning: this key holds the 'billing' scope" in result.stderr
    assert "no per-key budgets" not in result.stderr  # the row carried pod_visibility: the server is P235


@responses.activate
def test_create_with_billing_scope_on_a_server_without_the_scopes_route_still_warns(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys/scopes", status=404, json={"detail": "Not Found"})
    responses.add(responses.POST, f"{API}/keys", json=created("payer", scopes=["billing"]))

    result = run("keys", "create", "payer", "--scope", "billing")

    text = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Warning: this key holds the 'billing' scope" in text and "did not describe it" in text


@pytest.mark.parametrize(
    "args, wording",
    [
        (["--daily-budget", "0"], "x>=1.0"),
        (["--max-budget", "-5"], "x>=1.0"),
        (["--daily-budget", "abc"], "not a valid float"),
        (["--scope", "admin"], "'admin' is not one of"),
        (["--pod-visibility", "all"], "'all' is not one of"),
    ],
)
def test_create_refuses_a_bad_budget_scope_or_visibility_before_any_request(home, monkeypatch, args, wording):
    with responses.RequestsMock() as mocked:
        result = run("keys", "create", "k", *args)
        assert result.exit_code == 2, result.output
        assert wording in result.output
        assert len(mocked.calls) == 0


@pytest.mark.parametrize(
    "args, wording",
    [
        (["--daily-budget", "50", "--max-budget", "20"], "the max budget ($20.00) is below the daily budget ($50.00)"),
        (["--daily-budget", "50", "--monthly-budget", "20"], "the monthly budget ($20.00) is below the daily budget ($50.00)"),
        (["--monthly-budget", "300", "--max-budget", "200"], "the max budget ($200.00) is below the monthly budget ($300.00)"),
    ],
)
@responses.activate
def test_create_refuses_a_wider_budget_below_a_narrower_one(home, monkeypatch, args, wording):
    result = run("keys", "create", "k", *args, "--json")

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "invalid_arguments" and wording in error["message"]
    assert len(responses.calls) == 0

    fine = run("keys", "create", "k", "--daily-budget", "20", "--max-budget", "20", "--json")  # equal is allowed
    assert fine.exit_code != EXIT_CONFIGURATION_ERROR


# ------------------------------------------------------------------------------------------------- keys list / show / scopes
@responses.activate
def test_list_shows_scopes_budget_and_pod_count_per_key(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))

    result = run("keys", "list")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "API keys of Research (2 total)" in text
    assert "agent-1 read,rent $4.80/$20.00 today · $60.10/$300.00 month · $37.25/$200.00 total 1" in text
    assert "ops read,rent,manage — 0" in text  # no budget → —; the server's pods_count → 0
    assert not calls_to("/pods")  # the count is the row's `pods_count`, not a second read
    assert "sk_test_fixture" not in result.output  # the rows carry the key material; the table never prints it


@responses.activate
def test_list_json_carries_the_budget_fields_and_never_the_secret(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))

    result = run("keys", "list", "--json")

    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["name"] for r in rows] == ["agent-1", "ops"]
    assert rows[0]["daily_budget_usd"] == 20.0 and rows[0]["spent_today_usd"] == 4.8 and rows[0]["pod_visibility"] == "own"
    assert rows[0]["monthly_budget_usd"] == 300.0 and rows[0]["spent_month_usd"] == 60.1
    assert rows[0]["pods_count"] == 1 and rows[1]["pods_count"] == 0
    assert rows[1]["daily_budget_usd"] is None and rows[1]["pod_visibility"] == "account"
    assert all("key" not in r for r in rows)


@responses.activate
def test_list_on_a_server_before_budgets_shows_dashes_for_budget_and_pods(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=[ws_fixture("key_created")])  # the DAH-2944 row: no budget, no pods_count

    result = run("keys", "list")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert "ci read,rent,manage — —" in text


@responses.activate
def test_show_prints_what_the_key_can_do_from_the_scopes_route(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.GET, f"{API}/keys/{AGENT_KEY}/refusals", json=fixture("refusals"))
    monkeypatch.setattr("lium.cli.keys.command._today", lambda: "2026-09-21")

    result = run("keys", "show", "Agent-1")  # the name, case-insensitive
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert f"agent-1 ({AGENT_KEY})" in text
    assert "Budget $4.80/$20.00 today · $60.10/$300.00 month · $37.25/$200.00 total" in text
    # the ledger's newest row (the fixture lists them out of order), counted for the UTC day
    assert "Refused 3 today — last: pod extend $8.40, daily budget $20.00 reached" in text
    refusals = calls_to(f"/keys/{AGENT_KEY}/refusals", "GET")
    assert len(refusals) == 1 and refusals[0].request.headers["Authorization"] == f"Bearer {SESSION}"
    own = next(v for v in fixture("scopes")["pod_visibility"] if v["value"] == "own")["description"]
    assert f"Pod visibility own — {own}" in text
    assert "Active pods 1" in text
    can = {s["scope"]: s["can"] for s in scope_rows()}
    for line in can["read"] + can["rent"]:  # the server's "can" lines, one bullet each, for the scopes the key holds
        assert line in text, line
    assert "manage:" not in text and "billing:" not in text and can["manage"][0] not in text
    assert "lium ps --key agent-1" in text and "lium billing history --key agent-1" in text


@responses.activate
def test_show_json_has_can_do_and_pods_and_accepts_the_id(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.GET, f"{API}/keys/{OPS_KEY}/refusals", json={"refusals": []})

    result = run("keys", "show", OPS_KEY, "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["name"] == "ops" and payload["pods_count"] == 0 and "key" not in payload
    assert payload["monthly_budget_usd"] is None and payload["spent_month_usd"] == 112.4
    assert payload["refusals"] == [] and payload["refusals_today"] == 0
    can = {s["scope"]: s["can"] for s in scope_rows()}
    assert payload["can_do"] == [f"{scope}: {line}" for scope in ("read", "rent", "manage") for line in can[scope]]


@responses.activate
def test_show_json_refusals_are_the_ledgers_rows_newest_first(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.GET, f"{API}/keys/{AGENT_KEY}/refusals", json=fixture("refusals"))
    monkeypatch.setattr("lium.cli.keys.command._today", lambda: "2026-09-21")

    result = run("keys", "show", "agent-1", "--json")

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert [r["created_at"] for r in payload["refusals"]] == sorted((r["created_at"] for r in fixture("refusals")["refusals"]), reverse=True)
    assert payload["refusals"][0]["route"] == "pod extend" and payload["refusals"][-1]["window"] == "monthly"
    assert payload["refusals_today"] == 3
    assert payload["refusals"][0] == fixture("refusals")["refusals"][2]  # the server's row, untouched


@responses.activate
def test_show_with_no_refusals_says_none_and_without_the_route_says_the_server_has_no_ledger(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    responses.add(responses.GET, f"{API}/keys/{OPS_KEY}/refusals", json=[])  # a bare list is read like the wrapped body
    responses.add(responses.GET, f"{API}/keys/{AGENT_KEY}/refusals", status=404, json={"detail": "Not Found"})

    none = run("keys", "show", "ops")
    older = run("keys", "show", "agent-1")

    assert none.exit_code == 0 and "Refused none" in " ".join(none.output.split())
    text = " ".join(older.output.split())
    assert older.exit_code == 0, older.output
    assert "Refused — (this server has no refusal ledger; lium-platform#630, not released)" in text
    machine = run("keys", "show", "agent-1", "--json")
    assert json.loads(machine.stdout)["refusals"] is None and json.loads(machine.stdout)["refusals_today"] is None


@responses.activate
def test_show_on_a_server_without_the_scopes_route_lists_the_scope_names_alone(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/keys/scopes", status=404, json={"detail": "Not Found"})
    no_refusals_route()

    result = run("keys", "show", "ops", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["can_do"] == ["read", "rent", "manage"]


@responses.activate
def test_show_an_unknown_or_ambiguous_name_is_refused(home, monkeypatch):
    session(monkeypatch)
    twins = fixture("keys")
    twins[1]["name"] = "agent-1"
    responses.add(responses.GET, f"{API}/keys", json=twins)

    missing = run("keys", "show", "nobody", "--json")
    ambiguous = run("keys", "show", "agent-1", "--json")

    assert missing.exit_code == EXIT_API_ERROR and "No API key named 'nobody'" in json.loads(missing.stderr)["error"]["message"]
    error = json.loads(ambiguous.stderr)["error"]
    assert ambiguous.exit_code == EXIT_API_ERROR and "2 API keys are named 'agent-1'" in error["message"]
    assert AGENT_KEY in error["message"] and OPS_KEY in error["message"]


@responses.activate
def test_scopes_lists_every_scope_in_the_servers_words(home):
    me()
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))

    table = run("keys", "scopes")
    machine = run("keys", "scopes", "--json")
    text = " ".join(table.output.split())

    assert table.exit_code == 0, table.output
    assert "API key scopes (4 total)" in text
    for row in scope_rows():
        # the start of each description: a long cell wraps inside the table, the JSON below is checked whole
        default = "yes" if row["default"] else "no"
        assert f"{row['scope']} {default} {row['description'][:24]}" in text
        assert row["can"][0][:24] in text
    assert "Pod visibility (--pod-visibility): own: Only the pods this key creates." in text
    assert "made without --scope gets read, rent, manage; billing only when asked for" in text
    assert json.loads(machine.stdout) == fixture("scopes")  # the server's body whole, money_routes included
    # read with the API key: the scope list is the one /keys read that does not need a session
    assert all(c.request.headers.get("X-API-KEY") == "sk_test_default" for c in calls_to("/keys/scopes"))


@pytest.mark.parametrize(
    "status, body",
    [
        # lium.io on 21 Sep 2026: the path falls into the session-only GET /keys/{id} → 401 unauthorized
        (401, {"error": {"code": "unauthorized", "message": "Not authenticated", "hint": "", "request_id": "req-401"}, "message": "Not authenticated"}),
        (404, {"detail": "Not Found"}),
    ],
)
@responses.activate
def test_scopes_on_a_server_without_the_route_is_not_found_whatever_the_old_route_answered(home, status, body):
    responses.add(responses.GET, f"{API}/keys/scopes", status=status, json=body)

    result = run("keys", "scopes", "--json")

    assert result.exit_code == EXIT_API_ERROR
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "This server has no GET /keys/scopes yet (lium-platform#630, not released): scope descriptions are unavailable"
    assert "read, rent and manage" in error["hint"] and "lium-platform#630" in error["hint"]


# ------------------------------------------------------------------------------------------------- ps --key
@responses.activate
def test_ps_key_with_an_id_filters_server_side_without_a_session(home):
    responses.add(responses.GET, f"{API}/pods", json=pods_rented_by_agent())
    me()

    result = run("ps", "--key", AGENT_KEY, "--format", "json")

    assert result.exit_code == 0, result.output
    assert query_of(calls_to("/pods")[0]) == {"api_key_id": AGENT_KEY}
    rows = json.loads(result.stdout)
    assert rows[0]["api_key_id"] == AGENT_KEY and rows[0]["api_key_name"] == "agent-1"
    assert not calls_to("/keys")  # an id needs no lookup


@responses.activate
def test_ps_key_with_a_name_resolves_it_on_the_key_list_with_the_session(home, monkeypatch):
    monkeypatch.setenv("LIUM_SESSION_TOKEN", SESSION)
    me()
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.GET, f"{API}/pods", json=pods_rented_by_agent())

    result = run("ps", "--key", "agent-1")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    keys_call = calls_to("/keys", "GET")[0]
    assert keys_call.request.headers["Authorization"] == f"Bearer {SESSION}"
    assert keys_call.request.headers["X-Lium-Workspace-Id"] == RESEARCH
    assert query_of(calls_to("/pods")[0]) == {"api_key_id": AGENT_KEY}
    assert "trainer" in text and "pods rented through key agent-1" in text


@responses.activate
def test_ps_key_with_a_name_and_no_session_says_to_log_in_or_pass_the_id(home):
    result = run("ps", "--key", "agent-1", "--format", "json")

    assert result.exit_code == EXIT_API_ERROR
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "session_required"
    assert "pass the key id from `lium keys list`" in error["message"] and "lium workspaces login" in error["hint"]
    assert len(responses.calls) == 0


@responses.activate
def test_ps_without_key_sends_no_filter_and_keeps_the_json_shape(home):
    responses.add(responses.GET, f"{API}/pods", json=ws_fixture("pods_research"))
    me()

    result = run("ps", "--format", "json")

    assert result.exit_code == 0, result.output
    assert query_of(calls_to("/pods")[0]) == {}
    assert "api_key_id" not in json.loads(result.stdout)[0]  # the fixture's pod was not rented through a key


# ------------------------------------------------------------------------------------------------- billing history
@responses.activate
def test_billing_history_key_and_days_reach_the_statement_route(home):
    responses.add(responses.GET, f"{API}/billing/statement", json=fixture("statement"))
    me()

    result = run("billing", "history", "--key", AGENT_KEY, "--from", "2026-09-01", "--to", "2026-09-21")
    text = " ".join(result.output.split())

    assert result.exit_code == 0, result.output
    assert query_of(calls_to("/billing/statement")[0]) == {
        "api_key_id": AGENT_KEY, "start_day": "2026-09-01", "end_day": "2026-09-21",
    }
    assert f"Charges through key {AGENT_KEY}: 2 pods, 2026-09-01 to 2026-09-21" in text
    assert "trainer 1×H100 agent-1 13.5 h $32.40 2026-09-20 18:00 running" in text
    assert "eval 1×RTX 4090 agent-1 10.0 h $4.85 2026-09-19 10:00 2026-09-19 20:00" in text
    assert "Total $37.25" in text


@responses.activate
def test_billing_history_json_is_the_servers_statement(home):
    responses.add(responses.GET, f"{API}/billing/statement", json=fixture("statement"))

    result = run("billing", "history", "--format", "json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == fixture("statement")
    assert query_of(calls_to("/billing/statement")[0]) == {}


@responses.activate
def test_billing_history_with_nothing_charged_says_so(home):
    responses.add(responses.GET, f"{API}/billing/statement", json={"start_day": None, "end_day": None, "total": 0.0, "pods": []})
    me()

    result = run("billing", "history")

    assert result.exit_code == 0, result.output
    assert "No charges (all time)" in result.output


@pytest.mark.parametrize(
    "args, wording",
    [
        (["--from", "2026/09/01"], "--from must be a UTC day as YYYY-MM-DD"),
        (["--from", "2026-09-21", "--to", "2026-09-01"], "--to (2026-09-01) is before --from (2026-09-21)"),
    ],
)
def test_billing_history_refuses_bad_days_before_any_request(home, args, wording):
    with responses.RequestsMock() as mocked:
        result = run("billing", "history", *args, "--json")
        assert result.exit_code == EXIT_CONFIGURATION_ERROR
        assert wording in json.loads(result.stderr)["error"]["message"]
        assert len(mocked.calls) == 0


# ------------------------------------------------------------------------------------------------- 402 budget / 403 scope
BUDGET_MESSAGE = (
    "API key 'agent-1' has reached its daily budget of $20.00 ($20.00 billed). "
    "Raise or clear the budget on the key to rent with it again."
)
# services/api_key.py raise_if_budget_exceeded, through core/exception_handlers.py error_response: `error` names the
# code and the sentence (errors/codes.py classify reads them off the dict detail; no hint for this code), the dict detail
# itself is the top-level `message`
BUDGET_402 = {
    "error": {"code": "API_KEY_BUDGET_EXCEEDED", "message": BUDGET_MESSAGE, "hint": "", "request_id": "req-402-fixture"},
    "message": {
        "code": "API_KEY_BUDGET_EXCEEDED",
        "message": BUDGET_MESSAGE,
        "window": "daily",
        "budget_usd": 20.0,
        "spent_usd": 20.0,
        "api_key_id": AGENT_KEY,
    },
    "status_code": 402,
}


@responses.activate
def test_a_402_budget_refusal_is_a_typed_error_with_the_servers_plain_message():
    responses.add(responses.POST, f"{API}/executors/aa11bb22-cc33-4d44-8e55-ff6677889900/rent", status=402, json=BUDGET_402)
    lium = Lium(Config(api_key="sk_agent"))

    with pytest.raises(LiumBudgetExceededError) as raised:
        lium._request("POST", "/executors/aa11bb22-cc33-4d44-8e55-ff6677889900/rent", json={})

    error = raised.value
    # the server's sentence whole, then which key was refused (its fingerprint and source, never the key itself)
    assert str(error) == f"Budget exceeded: {BUDGET_MESSAGE} (key *** from explicit)"
    assert "sk_agent" not in str(error)
    assert error.code == "API_KEY_BUDGET_EXCEEDED" and error.request_id == "req-402-fixture"
    assert (error.window, error.budget_usd, error.spent_usd, error.api_key_id) == ("daily", 20.0, 20.0, AGENT_KEY)
    assert isinstance(error, LiumPermissionError)  # a caller that already handles the balance refusal handles this
    assert _classify_sdk_error(error) == ("budget_exceeded", EXIT_PERMISSION_DENIED)


@responses.activate
def test_a_402_reaches_the_cli_as_the_servers_code_exit_6_and_the_figures(home):
    responses.add(responses.GET, f"{API}/pods", status=402, json=BUDGET_402)

    result = run("ps", "--format", "json")

    assert result.exit_code == EXIT_PERMISSION_DENIED
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "API_KEY_BUDGET_EXCEEDED" and envelope["error"]["exit_code"] == EXIT_PERMISSION_DENIED
    assert envelope["error"]["message"].startswith("Budget exceeded: API key 'agent-1' has reached its daily budget")
    assert "lium keys show" in envelope["error"]["hint"] and "lium keys budget" in envelope["error"]["hint"]
    assert envelope["data"] == {
        "request_id": "req-402-fixture", "window": "daily", "budget_usd": 20.0, "spent_usd": 20.0, "api_key_id": AGENT_KEY,
    }


def budget_402(window: str, sentence: str) -> dict:
    body = json.loads(json.dumps(BUDGET_402))
    body["error"]["message"] = sentence
    body["message"].update({"message": sentence, "window": window})
    return body


MONTHLY_MESSAGE = "API key 'agent-1' has reached its monthly budget of $300.00 ($300.00 billed this month)."
MAX_MESSAGE = "API key 'agent-1' has reached its lifetime budget of $2,000.00 ($2,000.00 billed)."


@responses.activate
def test_the_402_names_the_window_hit_and_reads_the_same_from_rent_extend_fund_and_topup(home):
    """One mapping in the shared client (`_request` → `budget_error`), so a rent, a pod's schedule change (`rm
    --in`, what P235 calls extend), `lium fund` (`/tao/create-transfer`) and `lium topup create` (the invoice
    route; `topup card` of lium#272 goes through the same client) surface the identical sentence, naming the
    window: daily, monthly or max as the server said."""
    lium = Lium(Config(api_key="sk_agent"))
    cases = [
        ("POST", "/executors/aa11bb22-cc33-4d44-8e55-ff6677889900/rent", "daily", BUDGET_MESSAGE),
        ("POST", "/pods/0b6f2a7e-4c1d-4e0f-9a3b-2d5e6f708192/schedule-removal", "monthly", MONTHLY_MESSAGE),
        ("POST", "/tao/create-transfer", "max", MAX_MESSAGE),
        ("POST", "/tmc-pay/create-invoice", "monthly", MONTHLY_MESSAGE),
    ]
    for method, path, window, sentence in cases:
        responses.add(method, f"{API}{path}", status=402, json=budget_402(window, sentence))

    seen = []
    for method, path, window, sentence in cases:
        with pytest.raises(LiumBudgetExceededError) as raised:
            lium._request(method, path, json={})
        assert str(raised.value) == f"Budget exceeded: {sentence} (key *** from explicit)", path
        assert raised.value.window == window
        assert {"max": "lifetime"}.get(window, window) in str(raised.value)  # the window hit, in the server's word
        seen.append(str(raised.value).split(" (key")[0])
    assert seen[1] == seen[3]  # the schedule change and the top-up read word for word the same


def test_a_402_whose_sentence_does_not_name_the_window_gets_it_from_the_body():
    from lium.sdk.client import budget_error

    error = budget_error("Budget reached for this key", data={"window": "monthly", "budget_usd": 300, "spent_usd": 300})
    assert str(error) == "Budget exceeded: Budget reached for this key (monthly budget)"
    assert error.window == "monthly"
    named = budget_error("API key has reached its monthly budget", data={"window": "monthly"})
    assert str(named) == "Budget exceeded: API key has reached its monthly budget"  # not said twice


@responses.activate
def test_a_402_on_topup_create_reaches_the_cli_as_it_does_on_ps(home):
    responses.add(responses.POST, f"{API}/tmc-pay/create-invoice", status=402, json=budget_402("monthly", MONTHLY_MESSAGE))
    responses.add(responses.GET, f"{API}/pods", status=402, json=budget_402("monthly", MONTHLY_MESSAGE))

    topup = run("topup", "create", "-a", "20", "-c", "USDT", "-n", "tron", "--json")
    ps = run("ps", "--format", "json")

    assert topup.exit_code == EXIT_PERMISSION_DENIED == ps.exit_code
    topup_error, ps_error = json.loads(topup.stderr), json.loads(ps.stderr)
    assert topup_error["error"]["message"] == ps_error["error"]["message"]
    assert topup_error["error"]["message"].startswith(f"Budget exceeded: {MONTHLY_MESSAGE}")
    assert topup_error["error"]["code"] == "API_KEY_BUDGET_EXCEEDED" and topup_error["data"]["window"] == "monthly"
    assert topup_error["error"]["hint"] == ps_error["error"]["hint"]


@responses.activate
def test_a_402_on_a_pods_schedule_change_is_not_swallowed_as_a_failed_huid(home):
    """`rm --in` schedules one pod at a time and, before P235, wrote any failure to `failed` and moved on; a
    budget refusal is the key's, so it stops the run and prints the server's sentence like every other route."""
    me()
    responses.add(responses.GET, f"{API}/pods", json=ws_fixture("pods_research"))
    pod = ws_fixture("pods_research")[0]
    responses.add(responses.POST, f"{API}/pods/{pod['id']}/schedule-removal", status=402, json=budget_402("max", MAX_MESSAGE))

    result = run("rm", pod["pod_name"], "--in", "2h", "--yes", "--format", "json")

    assert result.exit_code == EXIT_PERMISSION_DENIED, result.output
    envelope = json.loads(next(line for line in result.stderr.splitlines() if line.startswith("{")))  # after the workspace line
    assert envelope["error"]["message"].startswith(f"Budget exceeded: {MAX_MESSAGE}")
    assert envelope["data"]["window"] == "max" and envelope["error"]["exit_code"] == EXIT_PERMISSION_DENIED


@responses.activate
def test_a_402_without_an_error_body_is_still_a_budget_error_with_no_figures(home):
    responses.add(responses.GET, f"{API}/pods", status=402, body="Payment Required")

    result = run("ps")

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert "Budget exceeded: Payment Required" in result.output


def test_a_403_naming_the_missing_scope_is_a_scope_error():
    error = permission_error("API key 'agent-1' does not have the 'manage' scope", key="sk_…", request_id="r1")

    assert isinstance(error, LiumScopeError) and error.scope == "manage" and error.request_id == "r1"
    assert str(error) == "Permission denied: API key 'agent-1' does not have the 'manage' scope (sk_…)"
    assert _classify_sdk_error(error) == ("missing_scope", EXIT_PERMISSION_DENIED)


def test_other_403s_are_still_plain_permission_errors():
    for detail in ("User is not verified", "Insufficient balance"):
        error = permission_error(detail)
        assert not isinstance(error, LiumScopeError), detail


@responses.activate
def test_a_scope_refusal_reaches_the_cli_with_the_scope_and_a_keys_hint(home):
    responses.add(responses.GET, f"{API}/pods", status=403, json={"detail": "API key 'agent-1' does not have the 'read' scope"})

    result = run("ps", "--format", "json")

    assert result.exit_code == EXIT_PERMISSION_DENIED
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "missing_scope" and envelope["data"] == {"scope": "read"}
    assert "lium keys create <name> --scope <scope>" in envelope["error"]["hint"]


# ------------------------------------------------------------------------------------------------- SDK
@responses.activate
def test_sdk_api_keys_create_defaults_and_validation():
    responses.add(responses.POST, f"{API}/keys", json=fixture("key_created_budget"))
    lium = Lium(Config(api_key="k", session_token=SESSION))

    key = lium.api_keys.create("agent-1", ["read", "rent"], daily_budget_usd=20, max_budget_usd=200, workspace_id=RESEARCH)
    default = lium.api_keys.create("plain", workspace_id=RESEARCH)

    first, second = (json.loads(c.request.body) for c in calls_to("/keys", "POST"))
    assert first == {"name": "agent-1", "scopes": ["read", "rent"], "pod_visibility": "own",
                     "daily_budget_usd": 20.0, "max_budget_usd": 200.0}
    assert second == {"name": "plain", "scopes": ["read", "rent", "manage"], "pod_visibility": "own"}
    assert key.key == "sk_test_fixture_key_not_a_secret_0000000000" and key.daily_budget_usd == 20.0
    assert default.matches("AGENT-1") and "key" not in key.to_dict()
    with pytest.raises(ValueError, match="at least \\$1"):
        lium.api_keys.create("bad", daily_budget_usd=0)
    with pytest.raises(ValueError, match="more than two decimals"):
        lium.api_keys.create("bad", max_budget_usd=20.005)
    with pytest.raises(ValueError, match="pod_visibility"):
        lium.api_keys.create("bad", pod_visibility="everyone")
    with pytest.raises(ValueError, match="at least one scope"):
        lium.api_keys.create("bad", [])
    with pytest.raises(ValueError, match="'billing' scope stands alone"):
        lium.api_keys.create("bad", ["billing", "rent"])
    with pytest.raises(ValueError, match="monthly budget .* is below the daily budget"):
        lium.api_keys.create("bad", daily_budget_usd=50, monthly_budget_usd=20)
    assert len(calls_to("/keys", "POST")) == 2  # the refused calls never went out

    lium.api_keys.create("payer", ["billing"], monthly_budget_usd=300, workspace_id=RESEARCH)
    assert json.loads(calls_to("/keys", "POST")[2].request.body) == {
        "name": "payer", "scopes": ["billing"], "pod_visibility": "own", "monthly_budget_usd": 300.0,
    }


def test_sdk_key_material_stays_out_of_repr_and_to_dict():
    """A traceback, a log line or `--json` that shows an ApiKeyInfo must not show the secret the row carried."""
    from lium.sdk.api_keys import _key

    key = _key(fixture("keys")[0])

    assert key.key == "sk_test_fixture_agent1_not_a_secret_0000000000"
    assert "sk_test" not in repr(key) and "sk_test" not in str(key)
    assert "key" not in key.to_dict() and key.to_dict()["pods_count"] == 1


@responses.activate
def test_sdk_workspaces_create_key_goes_through_the_same_default_scopes():
    """The pre-P235 SDK entry point mints the same key: never `billing` through the server's omitted-scopes default."""
    responses.add(responses.POST, f"{API}/keys", json=ws_fixture("key_created"))
    lium = Lium(Config(api_key="k", session_token=SESSION))

    row = lium.workspaces.create_key("ci", RESEARCH)

    assert json.loads(responses.calls[0].request.body)["scopes"] == ["read", "rent", "manage"]
    assert row["workspace_id"] == RESEARCH and row["key"]  # the raw row, as before


@responses.activate
def test_sdk_scopes_reads_a_bare_list_or_a_wrapped_one_and_ps_and_statement_pass_the_key():
    responses.add(responses.GET, f"{API}/keys/scopes", json=scope_rows())  # a bare list, as the brief first named it
    responses.add(responses.GET, f"{API}/pods", json=pods_rented_by_agent())
    responses.add(responses.GET, f"{API}/billing/statement", json=fixture("statement"))
    lium = Lium(Config(api_key="k"))

    scopes = lium.api_keys.scopes()
    pods = lium.ps(api_key_id=AGENT_KEY)
    statement = lium.billing_statement(api_key_id=AGENT_KEY, start_day="2026-09-01")

    assert [s.scope for s in scopes] == ["read", "rent", "manage", "billing"] and scopes[3].route_families
    assert [s.default for s in scopes] == [True, True, True, False] and scopes[0].title == "Read" and scopes[0].can
    assert lium.api_keys.pod_visibilities() == {}  # the bare list carries no pod-visibility rows
    assert pods[0].api_key_id == AGENT_KEY and pods[0].api_key_name == "agent-1"
    assert query_of(calls_to("/pods")[0]) == {"api_key_id": AGENT_KEY}
    assert query_of(calls_to("/billing/statement")[0]) == {"api_key_id": AGENT_KEY, "start_day": "2026-09-01"}
    assert statement["total"] == 37.25 and [p["pod_name"] for p in statement["pods"]] == ["trainer", "eval"]


@responses.activate
def test_sdk_key_routes_need_a_session_and_scopes_do_not():
    responses.add(responses.GET, f"{API}/keys/scopes", json=fixture("scopes"))
    lium = Lium(Config(api_key="k"))

    assert len(lium.api_keys.scopes()) == 4
    assert lium.api_keys.pod_visibilities() == {v["value"]: v["description"] for v in fixture("scopes")["pod_visibility"]}
    assert len(calls_to("/keys/scopes")) == 1  # read once per client
    responses.replace(responses.GET, f"{API}/keys/scopes", status=401, json={"detail": "Not authenticated"})
    with pytest.raises(LiumNotFoundError, match="no GET /keys/scopes yet"):
        Lium(Config(api_key="k")).api_keys.scopes()  # a server before P235: its GET /keys/{id} caught the path
    from lium.sdk import LiumSessionError

    for call in (
        lambda: lium.api_keys.list(RESEARCH),
        lambda: lium.api_keys.create("x"),
        lambda: lium.api_keys.get("id"),
        lambda: lium.api_keys.update("id", daily_budget_usd=5),
    ):
        with pytest.raises(LiumSessionError):
            call()


# ------------------------------------------------------------------------------------------------- keys budget (PATCH)
def patched_agent(**changes):
    row = {**fixture("keys")[0], **changes}
    return row


@responses.activate
def test_budget_sets_one_budget_and_leaves_the_other_alone(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.PATCH, f"{API}/keys/{AGENT_KEY}", json=patched_agent(daily_budget_usd=50.0))

    result = run("keys", "budget", "agent-1", "--daily-budget", "50")

    assert result.exit_code == 0, result.output
    patch = calls_to(f"/keys/{AGENT_KEY}", "PATCH")[0]
    assert json.loads(patch.request.body) == {"daily_budget_usd": 50.0}  # max_budget_usd is not named, so not sent
    assert patch.request.headers["Authorization"] == f"Bearer {SESSION}"
    assert patch.request.headers["X-Lium-Workspace-Id"] == RESEARCH
    assert "Budget of 'agent-1' is now $4.80/$50.00 today · $60.10/$300.00 month · $37.25/$200.00 total" in " ".join(result.output.split())


@responses.activate
def test_budget_sets_and_clears_the_monthly_window(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.PATCH, f"{API}/keys/{AGENT_KEY}", json=patched_agent(monthly_budget_usd=600.0))
    responses.add(responses.PATCH, f"{API}/keys/{AGENT_KEY}", json=patched_agent(monthly_budget_usd=None))

    setting = run("keys", "budget", "agent-1", "--monthly-budget", "600")
    clearing = run("keys", "budget", "agent-1", "--no-monthly-budget", "--json")

    assert setting.exit_code == 0 and clearing.exit_code == 0, setting.output + clearing.output
    first, second = (json.loads(c.request.body) for c in calls_to(f"/keys/{AGENT_KEY}", "PATCH"))
    assert first == {"monthly_budget_usd": 600.0} and second == {"monthly_budget_usd": None}
    assert "$60.10/$600.00 month" in " ".join(setting.output.split())
    assert json.loads(clearing.stdout)["monthly_budget_usd"] is None


@responses.activate
def test_budget_clears_a_budget_with_null_and_prints_the_row_as_json(home, monkeypatch):
    session(monkeypatch)
    responses.add(responses.GET, f"{API}/keys", json=fixture("keys"))
    responses.add(responses.PATCH, f"{API}/keys/{AGENT_KEY}", json=patched_agent(max_budget_usd=None))

    result = run("keys", "budget", AGENT_KEY, "--no-max-budget", "--daily-budget", "25", "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(calls_to(f"/keys/{AGENT_KEY}", "PATCH")[0].request.body) == {"daily_budget_usd": 25.0, "max_budget_usd": None}
    row = json.loads(result.stdout)
    assert row["max_budget_usd"] is None and row["name"] == "agent-1" and "key" not in row


@pytest.mark.parametrize(
    "args, wording",
    [
        ([], "Name what to change"),
        (["--daily-budget", "5", "--no-daily-budget"], "Set a budget or clear it, not both"),
        (["--daily-budget", "50", "--max-budget", "20"], "the max budget ($20.00) is below the daily budget ($50.00)"),
        (["--monthly-budget", "5", "--no-monthly-budget"], "Set a budget or clear it, not both"),
        (["--monthly-budget", "300", "--max-budget", "200"], "the max budget ($200.00) is below the monthly budget ($300.00)"),
    ],
)
def test_budget_refuses_a_contradiction_before_any_request(home, args, wording):
    with responses.RequestsMock() as mocked:
        result = run("keys", "budget", "agent-1", *args, "--json")
        assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
        error = json.loads(result.stderr)["error"]
        assert error["code"] == "invalid_arguments" and wording in error["message"]
        assert len(mocked.calls) == 0


def test_budget_below_a_dollar_is_a_usage_error(home):
    with responses.RequestsMock() as mocked:
        result = run("keys", "budget", "agent-1", "--max-budget", "0.5")
        assert result.exit_code == 2 and "x>=1.0" in result.output
        assert len(mocked.calls) == 0


@responses.activate
def test_sdk_update_sends_only_what_is_named_and_refuses_an_empty_change():
    responses.add(responses.PATCH, f"{API}/keys/{AGENT_KEY}", json=patched_agent(daily_budget_usd=None, max_budget_usd=300.0))
    lium = Lium(Config(api_key="k", session_token=SESSION))

    key = lium.api_keys.update(AGENT_KEY, daily_budget_usd=None, max_budget_usd=300, workspace_id=RESEARCH)

    assert json.loads(responses.calls[0].request.body) == {"daily_budget_usd": None, "max_budget_usd": 300.0}
    assert key.daily_budget_usd is None and key.max_budget_usd == 300.0
    with pytest.raises(ValueError, match="name a budget"):
        lium.api_keys.update(AGENT_KEY)
    with pytest.raises(ValueError, match="at least \\$1"):
        lium.api_keys.update(AGENT_KEY, max_budget_usd=0.99)
    with pytest.raises(ValueError, match="max budget .* is below the monthly budget"):
        lium.api_keys.update(AGENT_KEY, monthly_budget_usd=300, max_budget_usd=100)
    assert len(responses.calls) == 1
