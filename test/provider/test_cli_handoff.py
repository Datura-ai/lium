"""Human handoffs (a step only a person can do) against a local portal stub: Discord linking and e-mail confirmation.

The contract: ``human.handoff_required`` (exit 12) with ``data: {step, handoff_id, handoff_url, code, expires_at,
message_for_human}``; ``--wait`` polls until done (exit 0) or the code expires (``human.handoff_expired``, exit 12);
a portal without handoff sessions is ``portal.not_supported`` (exit 3) with the old browser URL in data.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from ._portal_stub import PortalStub, detail_response

TOKEN = "lpk_stub"
HANDOFF_ID = "hof_Zb3kq0Xw9yR2mT7vLp4sNg"
POLL_PATH = f"/auth/handoffs/{HANDOFF_ID}"
HANDOFF = {
    "handoff_id": HANDOFF_ID,
    "step": "discord_link",
    "handoff_url": "https://portal.example/handoff",
    "code": "ANNH-BD65",
    "expires_at": "2026-09-26T04:40:00Z",
    "message_for_human": "Open https://portal.example/handoff and enter code ANNH-BD65 to link Discord to your "
    "Lium provider account (expires 04:40 UTC).",
    "poll_url": POLL_PATH,
}


def created(step: str = "discord_link") -> dict:
    return detail_response({**HANDOFF, "step": step})


def polled(status: str, step: str = "discord_link") -> tuple[int, dict]:
    return 200, detail_response(
        {"handoff_id": HANDOFF_ID, "step": step, "status": status, "expires_at": HANDOFF["expires_at"]}
    )
OAUTH_URL = "https://discord.com/oauth2/authorize?client_id=stub"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    monkeypatch.setattr("lium.cli.provider.config._open_authorization_url", lambda url: False)
    stub = PortalStub()
    yield stub
    stub.close()


def run(portal: PortalStub, *args: str):
    env = {"LIUM_PROVIDER_TOKEN": TOKEN, "LIUM_PROVIDER_ACK": "", "LIUM_OUTPUT": ""}
    return CliRunner().invoke(provider_command, ["--portal-url", portal.url, *args], env=env)


def envelope(result, exit_code: int) -> dict:
    assert result.exit_code == exit_code, result.output
    return json.loads(result.stdout)


# --- connect-discord ---------------------------------------------------------------------------


def test_connect_discord_is_a_handoff_exit_12_with_the_url_and_code(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    error = envelope(run(portal, "--json", "config", "connect-discord"), 12)["error"]
    assert (error["code"], error["exit_code"]) == ("human.handoff_required", 12)
    assert {k: error["data"][k] for k in ("step", "handoff_url", "code", "expires_at", "message_for_human")} == {
        "step": "discord_link",
        "handoff_url": HANDOFF["handoff_url"],
        "code": "ANNH-BD65",
        "expires_at": HANDOFF["expires_at"],
        "message_for_human": HANDOFF["message_for_human"],
    }
    assert error["message"] == HANDOFF["message_for_human"]
    post = portal.requests[0]
    assert post["json"] == {"step": "discord_link"} and post["authorization"] == f"Bearer {TOKEN}"
    assert portal.calls("GET") == []


def test_connect_discord_in_a_terminal_prints_the_sentence_to_relay(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    result = run(portal, "config", "connect-discord")
    assert result.exit_code == 12
    assert HANDOFF["message_for_human"] in " ".join(result.stderr.split())


def test_connect_discord_wait_polls_until_the_person_is_done(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route_sequence("GET", POLL_PATH, polled("pending"), polled("claimed"), polled("completed"))
    result = run(portal, "--json", "config", "connect-discord", "--wait", "--poll-interval", "0.1")
    data = envelope(result, 0)["data"]
    assert data["done"] is True and data["step"] == "discord_link" and data["discord_connected"] is True
    assert len(portal.calls("GET")) == 3
    event = json.loads(result.stderr.strip().splitlines()[0])
    assert event["event"] == "handoff" and event["code"] == "ANNH-BD65"


def test_connect_discord_wait_ends_at_the_codes_expiry(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route_sequence("GET", POLL_PATH, polled("pending"), polled("expired"))
    args = ("--json", "config", "connect-discord", "--wait", "--poll-interval", "0.1", "--timeout", "5")
    error = envelope(run(portal, *args), 12)["error"]
    assert (error["code"], error["exit_code"]) == ("human.handoff_expired", 12)
    assert error["data"]["status"] == "expired" and error["data"]["code"] == "ANNH-BD65"


def test_connect_discord_wait_rides_out_a_portal_blip(portal) -> None:
    unavailable = (503, {"detail": {"code": "handoff_unavailable", "message": "Try again in a minute."}})
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route_sequence("GET", POLL_PATH, unavailable, (502, {"detail": "Bad Gateway"}), polled("completed"))
    args = ("--json", "config", "connect-discord", "--wait", "--poll-interval", "0.1", "--timeout", "10")
    data = envelope(run(portal, *args), 0)["data"]
    assert data["done"] is True and len(portal.calls("GET")) == 3


def test_connect_discord_wait_gives_up_on_a_portal_that_stays_down_at_the_timeout(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route("GET", POLL_PATH, {"detail": {"code": "handoff_unavailable", "message": "Try again."}}, status=503)
    args = ("--json", "config", "connect-discord", "--wait", "--poll-interval", "0.1", "--timeout", "1")
    error = envelope(run(portal, *args), 3)["error"]
    assert error["code"] == "portal.handoff_unavailable" and len(portal.calls("GET")) >= 2


def test_a_handoff_the_portal_no_longer_knows_has_expired(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route("GET", POLL_PATH, {"detail": {"code": "handoff_not_found", "message": "No such handoff (or it expired)."}},
                 status=404)
    args = ("--json", "config", "connect-discord", "--wait", "--poll-interval", "0.1", "--timeout", "5")
    error = envelope(run(portal, *args), 12)["error"]
    assert (error["code"], error["data"]["status"]) == ("human.handoff_expired", "not_found")


def test_connect_discord_wait_timeout_is_still_handoff_required(portal) -> None:
    portal.route("POST", "/auth/handoffs", created(), status=201)
    portal.route_sequence("GET", POLL_PATH, polled("pending"))
    error = envelope(run(portal, "--json", "config", "connect-discord", "--wait", "--timeout", "0"), 12)["error"]
    assert error["code"] == "human.handoff_required" and error["data"]["status"] == "pending"
    assert "waited_s" in error["data"]


def test_connect_discord_without_handoff_sessions_is_not_supported_with_the_old_url(portal) -> None:
    portal.route("GET", "/auth/me/discord/oauth-url", {"authorization_url": OAUTH_URL})
    error = envelope(run(portal, "--json", "config", "connect-discord"), 3)["error"]
    assert (error["code"], error["exit_code"]) == ("portal.not_supported", 3)
    assert error["data"]["legacy_flow"] is True
    assert error["data"]["legacy_browser_url"] == OAUTH_URL
    assert "old browser flow" in error["message"]


def test_connect_discord_already_linked_is_done(portal) -> None:
    portal.route(
        "POST", "/auth/handoffs",
        {"detail": {"code": "handoff_step_done", "message": "This step is already done for the account."}}, status=409,
    )
    data = envelope(run(portal, "--json", "config", "connect-discord"), 0)["data"]
    assert data["done"] is True and data["already_done"] is True


def test_a_handoff_answer_without_a_code_is_contract_drift(portal) -> None:
    portal.route("POST", "/auth/handoffs", detail_response({"handoff_id": HANDOFF_ID}), status=201)
    error = envelope(run(portal, "--json", "config", "connect-discord"), 3)["error"]
    assert error["legacy_code"] == "PORTAL_CONTRACT_DRIFT"
    assert error["data"]["missing"] == ["handoff_url", "code"]


# --- portal confirm-email ------------------------------------------------------------------------


def test_confirm_email_is_a_handoff_exit_12(portal) -> None:
    portal.route("POST", "/auth/handoffs", created("email_confirm"), status=201)
    error = envelope(run(portal, "--json", "portal", "confirm-email"), 12)["error"]
    assert error["code"] == "human.handoff_required" and error["data"]["step"] == "email_confirm"
    assert portal.requests[0]["json"] == {"step": "email_confirm"}


def test_confirm_email_wait_success(portal) -> None:
    portal.route("POST", "/auth/handoffs", created("email_confirm"), status=201)
    portal.route_sequence("GET", POLL_PATH, polled("claimed", "email_confirm"), polled("completed", "email_confirm"))
    data = envelope(run(portal, "--json", "portal", "confirm-email", "--wait", "--poll-interval", "0.1"), 0)["data"]
    assert data == {"step": "email_confirm", "done": True, "handoff_id": HANDOFF_ID}


def test_confirm_email_without_handoff_sessions_is_not_supported(portal) -> None:
    error = envelope(run(portal, "--json", "portal", "confirm-email"), 3)["error"]
    assert error["code"] == "portal.not_supported"
    assert error["data"]["legacy_browser_url"] == f"{portal.url}/settings"


def test_confirm_email_takes_no_code_option(portal) -> None:
    result = run(portal, "--json", "portal", "confirm-email", "--code", "123456")
    assert result.exit_code == 2 and "No such option '--code'" in result.output
    assert portal.requests == []


# --- Google sign-in is not for agents -----------------------------------------------------------


@pytest.mark.parametrize("args", [["portal", "login", "--help"], ["token", "--help"]])
def test_help_tells_agents_to_use_a_provider_token_not_google(args) -> None:
    result = CliRunner().invoke(provider_command, args)
    flat = " ".join(result.output.split())
    assert "Google sign-in is for people in the portal" in flat
    assert "LIUM_PROVIDER_TOKEN" in flat
