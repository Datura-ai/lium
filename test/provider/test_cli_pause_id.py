"""The pause proof: `node pause` says whether this call set the pause, and `node resume --pause-id` lifts only that pause.

`PausePortal` answers the portal's routes for one rented node the way the portal does: a pause sets
`new_rentals_pause_requested_at` and a fresh `pause_id` only when no pause is set, and says so in
`paused_by_this_call`; `DELETE …/new-rentals/pause?pause_id=` lifts the pause only while that id is the current one,
else 409 `PAUSE_ID_MISMATCH` with `detail.current_pause_id`, changing nothing. `older=True` is a portal from before
`pause_id`: no `pause_id` or `paused_by_this_call` anywhere, and the query parameter ignored (it resumes anyway).
"""

from __future__ import annotations

import json
import uuid

import pytest
from click.testing import CliRunner

from lium.cli.provider.command import provider_command
from ._agent_mode import AGENT_SWITCHES, PLAIN_TEXT, read_error
from ._portal_stub import PortalStub

TOKEN = "lpk_stub"
NODE = "7c1f0e2a-0000-4000-8000-000000000001"
PAUSE_PATH = f"/executors/{NODE}/new-rentals/pause"
NODE_PATH = f"/executors/{NODE}"


class PausePortal:
    def __init__(self, stub: PortalStub, *, older: bool = False) -> None:
        self.older = older
        self.pause_id: str | None = None
        self.requested_at: str | None = None
        self.reject_ids = False
        stub.handle("POST", PAUSE_PATH, self._pause)
        stub.handle("DELETE", PAUSE_PATH, self._resume)
        stub.handle("GET", NODE_PATH, lambda _request: (200, self.node()))
        stub.handle("GET", "/executors/listing", lambda _request: (200, [self.listing_row()]))

    def node(self) -> dict:
        record = {
            "id": NODE,
            "executor_ip_address": "203.0.113.10",
            "executor_ip_port": "8080",
            "price_per_gpu": 1.5,
            "rented": True,
            "new_rentals_pause_requested_at": self.requested_at,
        }
        if not self.older:
            record["pause_id"] = self.pause_id
        return record

    def listing_row(self) -> dict:
        row = {"id": NODE, "listing_state": "rented", "hidden_reasons": [], "gpu_count": 8, "rented_gpu_count": 8}
        if not self.older:
            row |= {"new_rentals_pause_requested_at": self.requested_at, "pause_id": self.pause_id}
        return row

    def paused(self) -> bool:
        return self.requested_at is not None

    def owner_pauses(self) -> str:
        """Someone else (the owner, in the portal) pauses first; the pause carries its own id."""
        assert not self.paused()
        self.pause_id, self.requested_at = str(uuid.uuid4()), "2026-09-26T13:00:00Z"
        return self.pause_id

    def platform_pauses(self) -> None:
        """A pause set without an id (from before ids existed, or by the platform itself)."""
        self.pause_id, self.requested_at = None, "2026-09-26T13:00:00Z"

    def owner_resumes(self) -> None:
        self.pause_id = self.requested_at = None

    def _pause(self, _request: dict) -> tuple[int, dict]:
        by_this_call = not self.paused()
        if by_this_call:
            self.pause_id, self.requested_at = str(uuid.uuid4()), "2026-09-26T14:00:00Z"
        if self.older:
            return 200, self.node()
        return 200, {**self.node(), "paused_by_this_call": by_this_call}

    def _resume(self, request: dict) -> tuple[int, dict]:
        wanted = (request["query"].get("pause_id") or [None])[0]
        if wanted is None or self.older:
            self.owner_resumes()
            return 200, self.node()
        try:
            uuid.UUID(wanted)
            if self.reject_ids:
                raise ValueError
        except ValueError:
            return 422, {"detail": [{"type": "uuid_parsing", "loc": ["query", "pause_id"], "msg": "Input should be a valid UUID"}]}
        if wanted != self.pause_id:
            current = self.pause_id
            return 409, {
                "detail": {
                    "code": "PAUSE_ID_MISMATCH",
                    "message": f"Node {NODE} was not resumed: pause {wanted} is not its current pause.",
                    "current_pause_id": current,
                }
            }
        self.owner_resumes()
        return 200, self.node()


@pytest.fixture
def stub(tmp_path, monkeypatch):
    monkeypatch.setattr("lium.provider.token_store.DEFAULT_TOKEN_PATH", tmp_path / "tokens.json")
    portal = PortalStub()
    yield portal
    portal.close()


@pytest.fixture
def portal(stub):
    return PausePortal(stub)


@pytest.fixture
def older(stub):
    return PausePortal(stub, older=True)


def run(stub: PortalStub, *args: str, env: dict | None = None):
    base_env = {"LIUM_PROVIDER_TOKEN": TOKEN, "LIUM_PROVIDER_ACK": "", "LIUM_PROVIDER_PASSWORD": "", **PLAIN_TEXT}
    return CliRunner().invoke(provider_command, ["--portal-url", stub.url, *args], env={**base_env, **(env or {})})


def run_under(stub: PortalStub, switch, *args: str):
    flags, env = switch
    return run(stub, *flags, *args, env=env)


def ok(result) -> dict:
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is True
    return envelope["data"]


def error(result, exit_code: int) -> dict:
    assert result.exit_code == exit_code, result.output
    envelope = json.loads(result.stdout)
    assert envelope["ok"] is False and envelope["error"]["exit_code"] == exit_code
    return envelope["error"]


def pause(stub: PortalStub) -> dict:
    return ok(run(stub, "--json", "node", "pause", NODE, "--yes"))


# --- node pause: whose pause is it ------------------------------------------------------------


def test_an_own_pause_is_paused_by_this_call_with_its_own_pause_id(stub, portal) -> None:
    data = pause(stub)
    assert data["paused_by_this_call"] is True
    assert data["pause_id"] == portal.pause_id and uuid.UUID(data["pause_id"])
    assert ok(run(stub, "--json", "node", "get", NODE))["pause_id"] == data["pause_id"]
    row = ok(run(stub, "--json", "node", "listing", NODE))
    assert (row["pause_id"], row["new_rentals_pause_requested_at"]) == (data["pause_id"], "2026-09-26T14:00:00Z")


def test_a_repeat_pause_is_not_by_this_call_and_names_the_first_pause(stub, portal) -> None:
    first = pause(stub)
    again = pause(stub)
    assert (again["paused_by_this_call"], again["pause_id"]) == (False, first["pause_id"])


def test_a_pause_after_the_owners_names_the_owners_pause_and_is_not_by_this_call(stub, portal) -> None:
    owners = portal.owner_pauses()
    data = pause(stub)
    assert (data["paused_by_this_call"], data["pause_id"]) == (False, owners)


def test_a_pause_after_one_set_without_an_id_answers_a_null_pause_id(stub, portal) -> None:
    portal.platform_pauses()
    data = pause(stub)
    assert data["paused_by_this_call"] is False and data["pause_id"] is None


def test_the_text_summary_of_a_repeat_pause_says_it_was_not_this_call(stub, portal) -> None:
    assert f"node {NODE}: new rentals paused" in run(stub, "node", "pause", NODE, "--yes").output
    assert f"node {NODE}: new rentals already paused, not by this call" in run(stub, "node", "pause", NODE, "--yes").output


# --- node resume --pause-id: resume only one's own pause -------------------------------------


def test_resume_with_the_own_pause_id_resumes_and_sends_the_id(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    ok(run(stub, "--json", "node", "resume", NODE, "--pause-id", own, "--yes"))
    assert not portal.paused()
    delete = [r for r in stub.requests if r["method"] == "DELETE"]
    assert [r["query"] for r in delete] == [{"pause_id": [own]}]
    assert stub.calls()[-2:] == [("GET", NODE_PATH), ("DELETE", PAUSE_PATH)]


def test_resume_sends_the_pause_id_in_canonical_form(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    ok(run(stub, "--json", "node", "resume", NODE, "--pause-id", own.replace("-", "").upper(), "--yes"))
    assert stub.requests[-1]["query"] == {"pause_id": [own]}


def test_resume_after_someone_resumed_and_paused_again_is_a_mismatch_and_changes_nothing(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    portal.owner_resumes()
    owners = portal.owner_pauses()
    err = error(run(stub, "--json", "node", "resume", NODE, "--pause-id", own, "--yes"), 3)
    assert (err["code"], err["legacy_code"]) == ("node.pause_id_mismatch", None)
    assert err["data"]["current_pause_id"] == owners
    assert (err["data"]["pause_id"], err["data"]["node_id"]) == (own, NODE)
    assert "Nothing changed" in err["hint"]
    assert (portal.pause_id, portal.paused()) == (owners, True)


def test_resume_after_someone_resumed_is_a_mismatch_with_a_null_current_pause_id(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    portal.owner_resumes()
    err = error(run(stub, "--json", "node", "resume", NODE, "--pause-id", own, "--yes"), 3)
    assert err["code"] == "node.pause_id_mismatch" and err["data"]["current_pause_id"] is None


def test_a_malformed_pause_id_is_input_arg_invalid_and_sends_nothing(stub, portal) -> None:
    err = error(run(stub, "--json", "node", "resume", NODE, "--pause-id", "not-a-uuid", "--yes"), 2)
    assert (err["code"], err["legacy_code"]) == ("input.arg_invalid", "ARG_INVALID")
    assert err["data"] == {"option": "--pause-id", "value": "not-a-uuid"}
    assert stub.requests == []


def test_the_portal_refusing_the_pause_id_422_is_input_arg_invalid(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    portal.reject_ids = True
    err = error(run(stub, "--json", "node", "resume", NODE, "--pause-id", own, "--yes"), 2)
    assert err["code"] == "input.arg_invalid" and err["data"]["value"] == own
    assert portal.paused()


def test_resume_without_a_pause_id_lifts_any_pause_without_reading_the_node(stub, portal) -> None:
    portal.owner_pauses()
    ok(run(stub, "--json", "node", "resume", NODE, "--yes"))
    assert not portal.paused()
    assert stub.calls() == [("DELETE", PAUSE_PATH)] and stub.requests[0]["query"] == {}


# --- an older portal: no pause_id anywhere ---------------------------------------------------


def test_resume_with_a_pause_id_on_an_older_portal_refuses_and_sends_nothing(stub, older) -> None:
    pause(stub)
    err = error(run(stub, "--json", "node", "resume", NODE, "--pause-id", str(uuid.uuid4()), "--yes"), 3)
    assert (err["code"], err["legacy_code"]) == ("portal.not_supported", None)
    assert err["data"]["unsupported"] == "pause_id" and "Nothing was sent" in err["hint"]
    assert stub.calls()[-1] == ("GET", NODE_PATH) and ("DELETE", PAUSE_PATH) not in stub.calls()
    assert older.paused()


def test_an_older_portal_leaves_pause_id_and_paused_by_this_call_null_not_false(stub, older) -> None:
    data = pause(stub)
    assert "paused_by_this_call" in data and data["paused_by_this_call"] is None
    assert "pause_id" in data and data["pause_id"] is None
    assert ok(run(stub, "--json", "node", "get", NODE))["pause_id"] is None
    row = ok(run(stub, "--json", "node", "listing", NODE))
    assert (row["pause_id"], row["new_rentals_pause_requested_at"]) == (None, None)
    listed = ok(run(stub, "--json", "node", "listing"))
    assert [(r["pause_id"], r["new_rentals_pause_requested_at"]) for r in listed] == [(None, None)]


def test_plain_text_node_get_on_an_older_portal_adds_no_pause_id_row(stub, older) -> None:
    result = run(stub, "node", "get", NODE)
    assert result.exit_code == 0, result.output
    assert "Pause Id" not in result.output and "New Rentals Pause Requested At" in result.output


# --- one exit map for all three agent-mode switches, and plain text ----------------------------


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_each_refusal_exits_the_same_under_every_agent_switch(stub, portal, switch) -> None:
    own = pause(stub)["pause_id"]
    portal.owner_resumes()
    portal.owner_pauses()
    cases = [
        (("--pause-id", own), 3, "node.pause_id_mismatch"),
        (("--pause-id", "nope"), 2, "input.arg_invalid"),
    ]
    for extra, exit_code, code in cases:
        result = run_under(stub, switch, "node", "resume", NODE, *extra, "--yes")
        assert result.exit_code == exit_code, result.output
        assert read_error(result, switch)[0] == code


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_the_older_portal_guard_exits_the_same_under_every_agent_switch(stub, older, switch) -> None:
    older.owner_pauses()
    result = run_under(stub, switch, "node", "resume", NODE, "--pause-id", str(uuid.uuid4()), "--yes")
    assert result.exit_code == 3, result.output
    assert read_error(result, switch)[0] == "portal.not_supported"
    assert ("DELETE", PAUSE_PATH) not in stub.calls()


@pytest.mark.parametrize("switch", AGENT_SWITCHES)
def test_a_matching_resume_succeeds_under_every_agent_switch(stub, portal, switch) -> None:
    own = pause(stub)["pause_id"]
    result = run_under(stub, switch, "node", "resume", NODE, "--pause-id", own, "--yes")
    assert result.exit_code == 0, result.output
    assert not portal.paused()


def test_plain_text_mismatch_prints_the_code_and_exits_3(stub, portal) -> None:
    own = pause(stub)["pause_id"]
    portal.owner_resumes()
    portal.owner_pauses()
    result = run(stub, "node", "resume", NODE, "--pause-id", own, "--yes")
    assert result.exit_code == 3, result.output
    assert result.stderr.splitlines()[0].startswith("[node.pause_id_mismatch] Node ")


def test_plain_text_malformed_pause_id_is_the_old_arg_invalid_exit_1(stub, portal) -> None:
    result = run(stub, "node", "resume", NODE, "--pause-id", "nope", "--yes")
    assert result.exit_code == 1, result.output
    assert result.stderr.startswith("[ARG_INVALID] --pause-id 'nope' is not a UUID")
    assert stub.requests == []


def test_resume_help_documents_the_pause_id_flag() -> None:
    result = CliRunner().invoke(provider_command, ["node", "resume", "--help"])
    assert result.exit_code == 0
    flat = " ".join(result.output.split())
    assert "--pause-id UUID" in flat and "node.pause_id_mismatch (exit 3)" in flat and "portal.not_supported" in flat
