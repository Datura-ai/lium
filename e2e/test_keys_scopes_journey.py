"""API-key scopes, per-key pod and charge filters, through the CLI against the live API, with the e2e account's
API key only: `lium keys scopes` needs no credential, `lium ps --key <id>` and `lium billing history --key <id>` take
an id without a session. The session-only commands (`keys create/list/show/budget`) need `lium workspaces login`, which
the e2e account does not have — they are driven by test/test_keys_budgets_cli.py on recorded bodies.

On a server without per-key budgets the scopes route is `not_found` and the per-key filter is ignored: those cases
skip and say so, so the suite is green on main today and proves the feature the day the server ships it.
"""

from __future__ import annotations

import json
import uuid

import pytest

from conftest import Session

NOBODYS_KEY = str(uuid.uuid4())  # an id no key of the account has: the filter must answer nothing


def scopes_payload(session: Session) -> dict | None:
    r = session.lium("keys", "scopes", "--json")
    if r.rc == 3 and json.loads(r.err or r.out)["error"]["code"] == "not_found":
        return None
    assert r.rc == 0, r
    return r.json()


def test_billing_history_json_is_the_ledger_statement(session: Session):
    r = session.lium("billing", "history", "--format", "json", check=True)
    statement = r.json()
    assert isinstance(statement.get("pods"), list) and isinstance(statement.get("total"), (int, float)), r
    for pod in statement["pods"]:
        assert {"pod_id", "total", "days"} <= set(pod), pod


def test_keys_scopes_are_the_servers_four_scopes_with_billing_off_by_default(session: Session):
    payload = scopes_payload(session)
    if payload is None:
        pytest.skip("this server has no GET /keys/scopes yet (per-key budgets are not released)")
    rows = {row["scope"]: row for row in payload["scopes"]}
    assert set(rows) == {"read", "rent", "manage", "billing"}, list(rows)
    assert rows["billing"]["default"] is False and all(rows[s]["default"] for s in ("read", "rent", "manage"))
    assert all(row["description"] and row["can"] and row["route_families"] for row in rows.values())
    assert {v["value"] for v in payload["pod_visibility"]} == {"own", "account"}
    table = session.lium("keys", "scopes", check=True)
    assert rows["billing"]["description"][:30] in table.out, table  # the table prints the server's sentence


def test_ps_and_billing_history_filter_by_key_id_server_side(session: Session):
    ps = session.lium("ps", "--key", NOBODYS_KEY, "--format", "json", check=True)
    history = session.lium("billing", "history", "--key", NOBODYS_KEY, "--format", "json", check=True)
    pods, charges = ps.json(), history.json()
    assert isinstance(pods, list) and isinstance(charges.get("pods"), list)
    if scopes_payload(session) is None:
        # the account's lists come back whole, and the CLI says so rather than label them one key's
        for result, rows in ((ps, pods), (history, charges["pods"])):
            assert not rows or "cannot filter by API key" in result.err, result
        pytest.skip("this server ignores api_key_id (per-key budgets are not released): the lists are the account's")
    assert pods == [] and charges["pods"] == [] and charges["total"] == 0, (pods, charges)
    assert "cannot filter" not in ps.err + history.err
