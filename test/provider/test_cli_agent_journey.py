"""The provider journey an agent runs unattended, end to end against a local portal stub.

Every command signs in with a synthetic ``LIUM_PROVIDER_TOKEN`` (no wallet), prints one ``--json`` envelope
with ``exit_code`` on failure, and exits by the unified map: 2 input, 3 portal, 4 network, 5 not found, 6 auth.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from ._portal_stub import PortalStub, closed_port_url

TOKEN = "lpk_stub"
HOTKEY = "5StubHotkeyForTheAgentJourneyTests"
NODE = "7c1f0e2a-0000-4000-8000-000000000001"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    stub = PortalStub()
    yield stub
    stub.close()


def run(portal: PortalStub | str, *args: str, env: dict | None = None):
    url = portal if isinstance(portal, str) else portal.url
    base_env = {"LIUM_PROVIDER_TOKEN": TOKEN, "LIUM_PROVIDER_ACK": "", "LIUM_OUTPUT": "", "LIUM_PROVIDER_PASSWORD": ""}
    return CliRunner().invoke(provider_command, ["--portal-url", url, *args], env={**base_env, **(env or {})})


def ok(result) -> object:
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    return envelope["data"]


def error(result, exit_code: int) -> dict:
    assert result.exit_code == exit_code, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False and envelope["error"]["exit_code"] == exit_code
    return envelope["error"]


# --- auth: LIUM_PROVIDER_TOKEN is the bearer token, no wallet needed --------------------------


def test_the_provider_token_env_is_sent_as_the_bearer_token(portal) -> None:
    portal.route("GET", "/executors/listing", [])
    ok(run(portal, "--json", "node", "listing"))
    assert portal.requests[0]["authorization"] == f"Bearer {TOKEN}"


def test_no_hotkey_and_no_token_names_the_token_env_in_the_hint(portal) -> None:
    err = error(run(portal, "--json", "node", "listing", env={"LIUM_PROVIDER_TOKEN": ""}), 1)
    assert err["legacy_code"] == "ARG_INVALID" and "LIUM_PROVIDER_TOKEN" in err["hint"]
    assert portal.requests == []


# --- node register-token / tier / pause / resume / listing -------------------------------------


def test_register_token_mints_the_token_and_prints_the_install_line(portal) -> None:
    portal.route(
        "POST",
        "/executors/register-token",
        {"success": True, "data": {"token": "rt_stub", "issued_at": "2026-09-26T03:00:00Z", "expires_at": "2026-09-26T04:00:00Z"}},
    )
    data = ok(run(portal, "--json", "node", "register-token", "--yes"))
    assert data["token"] == "rt_stub" and data["expires_at"] == "2026-09-26T04:00:00Z"
    assert data["install_command"].endswith("mine.sh | bash -s -- --register rt_stub")


def test_register_token_under_json_without_yes_asks_for_confirmation_and_mints_nothing(portal) -> None:
    err = error(run(portal, "--json", "node", "register-token"), 2)
    assert err["code"] == "input.confirmation_required"
    assert portal.requests == []


def test_tier_eligibility_returns_allowed_and_the_blockers(portal) -> None:
    blockers = [{"code": "rented", "message": "The node is rented."}]
    portal.route("GET", f"/executors/{NODE}/tier-change-eligibility", {"success": True, "data": {"allowed": False, "blockers": blockers}})
    assert ok(run(portal, "--json", "node", "tier", "eligibility", NODE)) == {"allowed": False, "blockers": blockers}


def test_tier_set_posts_the_tier(portal) -> None:
    portal.route("POST", f"/executors/{NODE}/update-tier", {"success": True, "data": {"id": NODE, "tier": "secure"}})
    assert ok(run(portal, "--json", "node", "tier", "set", NODE, "secure", "--yes"))["tier"] == "secure"
    assert portal.requests[0]["json"] == {"tier": "secure"}


def test_a_refused_tier_change_is_the_portals_own_code_with_its_blocker(portal) -> None:
    detail = {"message": "The node is rented.", "code": "tier_change_blocked", "blocker": "rented"}
    portal.route("POST", f"/executors/{NODE}/update-tier", {"detail": detail}, status=400)
    err = error(run(portal, "--json", "node", "tier", "set", NODE, "spot", "--yes"), 3)
    assert err["code"] == "portal.tier_change_blocked"
    assert err["message"] == "The node is rented."
    assert err["data"]["detail"] == {"blocker": "rented"}


def test_pause_and_resume_hit_the_new_rentals_pause_route(portal) -> None:
    path = f"/executors/{NODE}/new-rentals/pause"
    portal.route("POST", path, {"id": NODE, "new_rentals_pause_requested_at": "2026-09-26T03:00:00Z"})
    portal.route("DELETE", path, {"id": NODE, "new_rentals_pause_requested_at": None})
    ok(run(portal, "--json", "node", "pause", NODE, "--yes"))
    ok(run(portal, "--json", "node", "resume", NODE, "--yes"))
    assert portal.calls() == [("POST", path), ("DELETE", path)]


def test_pausing_an_idle_node_is_portal_node_not_rented(portal) -> None:
    detail = {"message": "Node must be rented before pausing new rentals.", "code": "node_not_rented"}
    portal.route("POST", f"/executors/{NODE}/new-rentals/pause", {"detail": detail}, status=400)
    assert error(run(portal, "--json", "node", "pause", NODE, "--yes"), 3)["code"] == "portal.node_not_rented"


def test_listing_shows_each_nodes_state_and_hidden_reasons(portal) -> None:
    rows = [
        {"id": NODE, "listing_state": "hidden", "hidden_reasons": ["price_above_p90"]},
        {"id": "other", "listing_state": "listed", "hidden_reasons": []},
    ]
    portal.route("GET", "/executors/listing", rows)
    assert ok(run(portal, "--json", "node", "listing")) == rows
    assert ok(run(portal, "--json", "node", "listing", NODE)) == rows[0]


def test_listing_a_node_that_is_not_yours_is_node_not_found_exit_5(portal) -> None:
    portal.route("GET", "/executors/listing", [])
    assert error(run(portal, "--json", "node", "listing", NODE), 5)["code"] == "node.not_found"


# --- earnings / idle-pay / ledger ---------------------------------------------------------------


def test_earnings_reads_the_accounts_hotkey_then_its_daily_rows(portal) -> None:
    portal.route("GET", "/auth/me", {"miner_hotkey": HOTKEY})
    rows = {"rows": [{"date": "2026-09-25", "net_usd": 12.5}]}
    portal.route("GET", f"/provider-earnings/{HOTKEY}/daily", {"success": True, "data": rows})
    data = ok(run(portal, "--json", "earnings", "--from", "2026-09-20", "--to", "2026-09-25", "--node", NODE))
    assert data == rows
    request = portal.requests[-1]
    assert request["query"] == {"from": ["2026-09-20"], "to": ["2026-09-25"], "executor_ids": [NODE]}


def test_earnings_emissions_and_another_hotkey(portal) -> None:
    portal.route("GET", "/provider-earnings/5Other/emissions/daily", {"success": True, "data": {"rows": []}})
    ok(run(portal, "--json", "earnings", "--emissions", "--miner-hotkey", "5Other"))
    assert portal.calls() == [("GET", "/provider-earnings/5Other/emissions/daily")]


def test_a_bad_date_is_a_usage_error_before_any_call(portal) -> None:
    result = run(portal, "--json", "earnings", "--from", "25/09/2026")
    assert result.exit_code == 2 and portal.requests == []


OVERVIEW = {
    "as_of": "2026-09-26T03:00:00Z",
    "nodes": {"idle": 2, "idle_earning": 1, "idle_unpaid": 1},
    "earned": {"window_days": 30, "idle_pay_usd": 4.2},
    "node_rows": [
        {"executor_id": NODE, "gpu_label": "H100", "gpu_count": 8, "idle_pay": "not_paid",
         "idle_pay_reasons": [{"code": "nvidia_driver_below_minimum", "context": {}, "message": "Driver too old"}],
         "idle_pay_checked_at": "2026-09-26T02:50:00Z", "address": "ignored"},
        {"executor_id": "n-2", "gpu_label": "A100", "gpu_count": 1, "idle_pay": "paid", "idle_pay_reasons": []},
    ],
}


def test_idle_pay_lists_each_nodes_state_and_the_validators_reasons(portal) -> None:
    portal.route("GET", "/miners/overview", {"success": True, "data": OVERVIEW})
    data = ok(run(portal, "--json", "idle-pay"))
    assert (data["idle_pay_usd"], data["idle_earning"], data["idle_unpaid"]) == (4.2, 1, 1)
    assert [n["idle_pay"] for n in data["nodes"]] == ["not_paid", "paid"]
    assert "address" not in data["nodes"][0]
    one = ok(run(portal, "--json", "idle-pay", NODE))
    assert one["idle_pay_reasons"][0]["code"] == "nvidia_driver_below_minimum"


def test_idle_pay_for_a_node_that_is_not_yours_is_exit_5(portal) -> None:
    portal.route("GET", "/miners/overview", {"success": True, "data": OVERVIEW})
    assert error(run(portal, "--json", "idle-pay", "nope"), 5)["code"] == "node.not_found"


def test_idle_pay_for_an_account_without_the_overview_passes_the_portal_code_through(portal) -> None:
    detail = {"message": "This account's earnings are on its Overview page.", "code": "overview_not_for_custodied_account"}
    portal.route("GET", "/miners/overview", {"detail": detail}, status=403)
    assert error(run(portal, "--json", "idle-pay"), 6)["code"] == "portal.overview_not_for_custodied_account"


def test_ledger_passes_the_range(portal) -> None:
    portal.route("GET", "/provider-ledger/daily", {"success": True, "data": {"rows": [{"date": "2026-09-25", "kind": "rental"}]}})
    assert ok(run(portal, "--json", "ledger", "--from", "2026-09-01", "--to", "2026-09-25"))["rows"][0]["kind"] == "rental"
    assert portal.requests[0]["query"] == {"from": ["2026-09-01"], "to": ["2026-09-25"]}


# --- portal login --email -----------------------------------------------------------------------


def test_email_login_uses_the_password_env_and_later_commands_use_the_session(portal) -> None:
    portal.route("POST", "/auth/login-email", {"success": True, "data": {"miner": {"id": "m-1", "miner_hotkey": HOTKEY}, "token": "session-stub"}})
    portal.route("GET", "/executors/listing", [])
    env = {"LIUM_PROVIDER_TOKEN": "", "LIUM_PROVIDER_PASSWORD": "pw-stub"}
    data = ok(run(portal, "--json", "portal", "login", "--email", "agent@example.invalid", env=env))
    assert data == {"email": "agent@example.invalid", "provider_id": "m-1", "hotkey": HOTKEY, "token_present": True}
    assert portal.requests[0]["json"] == {"email": "agent@example.invalid", "password": "pw-stub"}
    assert portal.requests[0]["authorization"] is None

    ok(run(portal, "--json", "node", "listing", env={"LIUM_PROVIDER_TOKEN": ""}))
    assert portal.requests[-1]["authorization"] == "Bearer session-stub"


def test_email_login_without_the_password_env_is_input_required_not_a_prompt(portal) -> None:
    err = error(run(portal, "--json", "portal", "login", "--email", "agent@example.invalid"), 2)
    assert err["code"] == "input.input_required" and "LIUM_PROVIDER_PASSWORD" in err["hint"]
    assert portal.requests == []


def test_a_wrong_password_is_an_auth_error(portal) -> None:
    portal.route("POST", "/auth/login-email", {"detail": "Invalid credentials"}, status=401)
    err = error(run(portal, "--json", "portal", "login", "--email", "agent@example.invalid", env={"LIUM_PROVIDER_PASSWORD": "x"}), 2)
    assert err["legacy_code"] == "PORTAL_AUTH_INVALID"


# --- provider API tokens ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [("token", "list"), ("token", "create", "--name", "ci", "--scope", "read", "--yes"), ("token", "revoke", "t-1", "--yes")],
)
def test_token_commands_say_not_supported_until_the_portal_serves_them(portal, args) -> None:
    err = error(run(portal, "--json", *args), 3)
    assert err["code"] == "portal.not_supported"


def test_token_create_list_and_revoke_once_the_portal_serves_them(portal) -> None:
    portal.route("POST", "/auth/api-tokens", {"success": True, "data": {"id": "t-1", "token": "lpk_new_stub", "scopes": ["read", "node"]}})
    portal.route("GET", "/auth/api-tokens", {"success": True, "data": {"tokens": [{"id": "t-1", "name": "ci"}]}})
    portal.route("DELETE", "/auth/api-tokens/t-1", {"success": True, "data": {"id": "t-1", "revoked": True}})
    created = ok(run(portal, "--json", "token", "create", "--name", "ci", "--scope", "read", "--scope", "node", "--expires-days", "30", "--yes"))
    assert created["token"] == "lpk_new_stub"
    assert portal.requests[0]["json"] == {"name": "ci", "scopes": ["read", "node"], "expires_in_days": 30}
    assert ok(run(portal, "--json", "token", "list"))["tokens"][0]["id"] == "t-1"
    assert ok(run(portal, "--json", "token", "revoke", "t-1", "--yes"))["revoked"] is True


def test_token_revoke_without_yes_under_json_revokes_nothing(portal) -> None:
    assert error(run(portal, "--json", "token", "revoke", "t-1"), 2)["code"] == "input.confirmation_required"
    assert portal.requests == []


# --- network and portal error codes --------------------------------------------------------------


def test_connection_refused_is_net_unreachable_exit_4(portal) -> None:
    err = error(run(closed_port_url(), "--json", "node", "listing"), 4)
    assert err["code"] == "net.unreachable"


def test_a_coded_404_is_exit_5_and_a_coded_401_exit_6(portal) -> None:
    portal.route("GET", f"/executors/{NODE}/tier-change-eligibility", {"detail": {"message": "Not found node", "code": "node_not_found"}}, status=404)
    assert error(run(portal, "--json", "node", "tier", "eligibility", NODE), 5)["code"] == "portal.node_not_found"
    portal.route("GET", "/executors/listing", {"detail": {"message": "Token revoked", "code": "token_revoked"}}, status=401)
    assert error(run(portal, "--json", "node", "listing"), 6)["code"] == "portal.token_revoked"


def test_an_uncoded_portal_error_keeps_its_old_exit_status(portal) -> None:
    portal.route("GET", "/executors/listing", {"detail": "boom"}, status=500)
    err = error(run(portal, "--json", "node", "listing"), 3)
    assert (err["code"], err["legacy_code"]) == ("portal.server_error", "PORTAL_SERVER_ERROR")


def test_text_mode_prints_the_code_on_stderr(portal) -> None:
    result = run(closed_port_url(), "node", "listing")
    assert result.exit_code == 4
    assert "[net.unreachable]" in result.stderr and result.stdout == ""
