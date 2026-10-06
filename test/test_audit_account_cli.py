"""`lium audit --account`: the account audit log (GET /account/audit, lium-platform DAH-3245) — one entry per
request that changed something, with the client and the IP it came from — and `Lium.audit_log`, the SDK page reader.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.audit import command as audit_module
from lium.cli.cli import cli
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_PERMISSION_DENIED
from lium.sdk import Lium
from lium.sdk.exceptions import LiumNotFoundError, LiumPermissionError

KEY_ID = "323d3f8e-0150-44cb-ae1f-ba11071b2539"
POD_ID = "dc4919ac-b6aa-4890-b480-4633cdbf8114"

# the entry shape GET /account/audit answered in lium-platform#258's Ran-it run (a DELETE /pods/{id} by an API key from
# the CLI); the second is a member's entry as an owner sees it — no ip — and a refused request
ENTRIES_NEWEST_FIRST = [
    {
        "id": "ee2a0782-dbf1-4bf6-82d9-ca1227b3632e",
        "created_at": "2026-09-08T23:59:22.069873",
        "action": "pod.delete",
        "source": "cli",
        "actor": {
            "auth": "api_key",
            "user_id": "c7ad12ec-8fc5-407a-876d-c759e5b5260a",
            "api_key_id": KEY_ID,
            "api_key_name": "ci-runner",
        },
        "workspace_id": None,
        "ip": "203.0.113.9",
        "user_agent": "lium-cli/0.0.37",
        "request_id": "86943ca082d14405bacf58bc895e3a67",
        "method": "DELETE",
        "route": "/pods/{id}",
        "status_code": 200,
        "resource_type": "pod",
        "resource_id": POD_ID,
        "summary": {"path": {"id": POD_ID}},
    },
    {
        "id": "e2",
        "created_at": "2026-09-08T22:10:00",
        "action": "key.create",
        "source": "portal",
        "actor": {
            "auth": "session",
            "user_id": "u2",
            "api_key_id": None,
            "api_key_name": None,
        },
        "workspace_id": "w1",
        "ip": None,
        "user_agent": "Mozilla/5.0",
        "request_id": "r2",
        "method": "POST",
        "route": "/keys",
        "status_code": 200,
        "resource_type": "key",
        "resource_id": "k2",
        "summary": {"path": {}, "result": {"id": "k2", "name": "laptop"}},
    },
    {
        "id": "e3",
        "created_at": "2026-09-08T22:00:00",
        "action": "pod.delete",
        "source": "sdk",
        "actor": {
            "auth": "api_key",
            "user_id": "u3",
            "api_key_id": "k3",
            "api_key_name": "agent",
        },
        "workspace_id": None,
        "ip": "203.0.113.41",
        "user_agent": "python-requests/2.32",
        "request_id": "r3",
        "method": "DELETE",
        "route": "/pods/{id}",
        "status_code": 404,
        "resource_type": "pod",
        "resource_id": "0b6f2a7e-4c1d-4e0f-9a3b-2d5e6f708192",
        "summary": {"path": {"id": "0b6f2a7e-4c1d-4e0f-9a3b-2d5e6f708192"}},
    },
]


class _FakeLium:
    page: dict = {}
    calls: list = []
    pods: list = []
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def audit_log(self, **kwargs):
        _FakeLium.calls.append(kwargs)
        if self.error:
            raise self.error
        return dict(self.page)

    def events(self, **kwargs):  # the pod log must not be asked for with --account
        raise AssertionError("events() called in --account mode")


def _run(
    monkeypatch,
    *args,
    items=ENTRIES_NEWEST_FIRST,
    next_cursor=None,
    pods=(),
    error=None,
):
    _FakeLium.page = {"items": list(items), "next_cursor": next_cursor}
    _FakeLium.calls = []
    _FakeLium.pods = list(pods)
    _FakeLium.error = error
    monkeypatch.setattr(audit_module, "Lium", _FakeLium)
    monkeypatch.setattr(audit_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["audit", "--account", *args])


def test_rows_read_oldest_first_with_client_and_ip():
    rows = audit_module.account_rows(ENTRIES_NEWEST_FIRST)

    assert rows == [
        [
            "2026-09-08 22:00:00Z",
            "pod: delete (refused, 404)",
            "0b6f2a7e",
            "key agent (k3)",
            "sdk",
            "203.0.113.41",
        ],
        [
            "2026-09-08 22:10:00Z",
            "key: create",
            "laptop",
            "session u2",
            "portal",
            "—",
        ],  # a team-mate's entry: no ip; the By column names the member
        [
            "2026-09-08 23:59:22Z",
            "pod: delete",
            "dc4919ac",
            "key ci-runner (323d3f8e)",
            "cli",
            "203.0.113.9",
        ],
    ]


def test_table_prints_the_rows_in_order_and_never_asks_the_pod_log(monkeypatch):
    result = _run(monkeypatch)

    assert result.exit_code == 0, result.output
    out = result.output
    # Rich wraps cells to the runner's 80 columns, so order is checked on the whole output
    assert out.index("0b6f2a7e") < out.index("laptop") < out.index("dc4919ac")
    assert "IP" in out and "Client" in out and "Older entries" not in out


def test_json_prints_the_page_as_returned(monkeypatch):
    result = _run(monkeypatch, "--json", next_cursor="e3")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "items": ENTRIES_NEWEST_FIRST,
        "next_cursor": "e3",
    }


def test_filters_reach_the_sdk_and_a_pod_becomes_the_resource(monkeypatch):
    pods = [
        SimpleNamespace(
            id=POD_ID, huid="brave-fox-12", name="agent-pod", status="RUNNING"
        )
    ]
    monkeypatch.setattr(
        audit_module,
        "parse_targets",
        lambda target, listed: [p for p in listed if p.name == target],
    )

    result = _run(
        monkeypatch,
        "--action",
        "pod.",
        "--source",
        "cli",
        "--key",
        KEY_ID,
        "--pod",
        "agent-pod",
        "--since",
        "24h",
        "--limit",
        "50",
        pods=pods,
    )

    assert result.exit_code == 0, result.output
    [call] = _FakeLium.calls
    assert (
        call["action"] == "pod."
        and call["source"] == "cli"
        and call["api_key_id"] == KEY_ID
    )
    assert call["resource_id"] == POD_ID and call["limit"] == 50
    assert (
        datetime.now(timezone.utc) - call["since"]
    ).total_seconds() == pytest.approx(24 * 3600, abs=60)


def test_a_full_page_names_the_cursor_for_the_next_page(monkeypatch):
    result = _run(monkeypatch, next_cursor="e3")

    assert result.exit_code == 0, result.output
    assert "Older entries may exist" in result.output
    assert "--cursor" in result.output and "e3" in result.output


def test_cursor_reaches_the_sdk(monkeypatch):
    result = _run(monkeypatch, "--cursor", "e3")

    assert result.exit_code == 0, result.output
    [call] = _FakeLium.calls
    assert call["cursor"] == "e3"


def test_an_empty_cursor_page_says_no_older_entries(monkeypatch):
    result = _run(monkeypatch, "--cursor", "e3", items=[])

    assert result.exit_code == 0, result.output
    assert "No older entries" in result.output


def test_cursor_needs_account(monkeypatch):
    monkeypatch.setattr(audit_module, "Lium", _FakeLium)
    monkeypatch.setattr(audit_module, "ensure_config", lambda: None)
    _FakeLium.calls = []

    result = CliRunner().invoke(cli, ["audit", "--cursor", "e3"])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR, result.output
    assert "--cursor need --account" in result.output
    assert _FakeLium.calls == []


def test_no_entries_says_so(monkeypatch):
    result = _run(monkeypatch, "--since", "1h", items=[])

    assert result.exit_code == 0, result.output
    assert "No account activity recorded in that window." in result.output


def test_action_and_source_need_account(monkeypatch):
    monkeypatch.setattr(audit_module, "Lium", _FakeLium)
    monkeypatch.setattr(audit_module, "ensure_config", lambda: None)

    result = CliRunner().invoke(cli, ["audit", "--action", "pod."])

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--action, --source and --cursor need --account" in result.output


def test_an_unknown_source_is_refused_by_click(monkeypatch):
    result = _run(monkeypatch, "--source", "spaceship")

    assert result.exit_code == 2 and "spaceship" in result.output


def test_limit_above_500_is_refused_before_any_request(monkeypatch):
    result = _run(monkeypatch, "--limit", "501")

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "at most 500" in result.output and _FakeLium.calls == []


def test_an_old_backend_is_named(monkeypatch):
    result = _run(monkeypatch, error=LiumNotFoundError("Resource not found: Not Found"))

    assert result.exit_code == EXIT_API_ERROR
    assert "no account audit log (GET /account/audit)" in result.output


def test_a_403_mentions_the_read_scope(monkeypatch):
    # the SDK raises LiumPermissionError for the server's 403 (`require_api_key_scope`), not an auth error
    result = _run(
        monkeypatch,
        error=LiumPermissionError("Permission denied: API key 'renter' does not have the 'read' scope"),
    )

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert "`read` scope" in result.output


def test_a_403_keeps_the_servers_code_hint_and_request_id(monkeypatch):
    # the server's code, hint and request_id (DAH-3057) ride along under audit's own line, like on the 401
    error = LiumPermissionError("Permission denied: API key 'renter' does not have the 'read' scope",
                                code="missing_scope", hint="Create a key with the read scope.", request_id="req-403-0001")
    result = _run(monkeypatch, error=error)

    assert result.exit_code == EXIT_PERMISSION_DENIED
    assert "`read` scope" in result.output
    assert "Create a key with the read scope." in result.output and "request_id: req-403-0001" in result.output

    result = _run(monkeypatch, "--json", error=error)
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "missing_scope"
    assert envelope["error"]["hint"] == "Create a key with the read scope."
    assert envelope["data"] == {"request_id": "req-403-0001"}


# --------------------------------------------------------------------------------------------------
# the SDK method
# --------------------------------------------------------------------------------------------------
def _lium_with(monkeypatch, payload):
    lium = Lium.__new__(Lium)
    calls = []

    def fake_request(method, endpoint, params=None, **kwargs):
        calls.append((method, endpoint, params))
        return SimpleNamespace(json=lambda: payload)

    monkeypatch.setattr(lium, "_request", fake_request, raising=False)
    return lium, calls


def test_sdk_audit_log_sends_the_filters_and_returns_the_page(monkeypatch):
    lium, calls = _lium_with(
        monkeypatch, {"items": ENTRIES_NEWEST_FIRST[:1], "next_cursor": "c1"}
    )
    since = datetime(2026, 9, 8, tzinfo=timezone.utc)

    page = lium.audit_log(
        since=since,
        action="pod.",
        source="cli",
        api_key_id=KEY_ID,
        resource_id=POD_ID,
        cursor="c0",
        limit=50,
    )

    assert page == {"items": ENTRIES_NEWEST_FIRST[:1], "next_cursor": "c1"}
    [(method, endpoint, params)] = calls
    assert (method, endpoint) == ("GET", "/account/audit")
    assert params == {
        "limit": 50,
        "since": "2026-09-08T00:00:00+00:00",
        "action": "pod.",
        "source": "cli",
        "api_key_id": KEY_ID,
        "resource_id": POD_ID,
        "cursor": "c0",
    }


def test_sdk_audit_log_defaults_and_an_unexpected_body(monkeypatch):
    lium, calls = _lium_with(monkeypatch, [])

    page = lium.audit_log()

    assert page == {"items": [], "next_cursor": None}
    assert calls[0][2] == {"limit": 100}
