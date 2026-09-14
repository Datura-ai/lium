"""`lium audit`: the account's event log with the session or API key behind each action.

The backend records the actor on every request-side event and serves the account log at
GET /users/me/events (lium-platform DAH-2934); this command reads it, oldest first.
"""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.audit import command as audit_module
from lium.cli.cli import cli
from lium.cli.utils import EXIT_API_ERROR, EXIT_CONFIGURATION_ERROR, EXIT_POD_NOT_FOUND
from lium.sdk import Lium
from lium.sdk.exceptions import LiumAuthError

POD_ID = "0b6f2a7e-4c1d-4e0f-9a3b-2d5e6f708192"
KEY_ID = "3f2a9c11-0000-4000-8000-000000000001"


def _event(sub, *, created_at, actor=None, pod_name="my-pod", **fields):
    return {
        "id": "e",
        "created_at": created_at,
        "event_type": sub.split(".")[0],
        "sub_event_type": sub,
        "pod_id": POD_ID,
        "pod_name": pod_name,
        "actor": actor,
        "from_status": None,
        "to_status": None,
        "reason": None,
        "detail": None,
        "error": None,
        **fields,
    }


KEY_ACTOR = {"auth": "api_key", "api_key_id": KEY_ID, "api_key_name": "ci"}
EVENTS_NEWEST_FIRST = [
    _event("pod-lifecycle.status", created_at="2026-09-06T05:00:00", actor=KEY_ACTOR, to_status="DELETING",
           from_status="RUNNING", reason="user_initiated"),
    _event("pod-create.success", created_at="2026-09-06T04:02:00"),
    _event("pod-rent.requested", created_at="2026-09-06T04:00:00", actor={"auth": "session"}),
]


class _FakeLium:
    events_result: list = []
    calls: list = []
    pods: list = []
    error: Exception | None = None

    def __init__(self, *args, **kwargs):
        pass

    def ps(self):
        return list(self.pods)

    def events(self, **kwargs):
        _FakeLium.calls.append(kwargs)
        if self.error:
            raise self.error
        return list(self.events_result)


def _run(monkeypatch, *args, events=EVENTS_NEWEST_FIRST, pods=(), error=None):
    _FakeLium.events_result = list(events)
    _FakeLium.calls = []
    _FakeLium.pods = list(pods)
    _FakeLium.error = error
    monkeypatch.setattr(audit_module, "Lium", _FakeLium)
    monkeypatch.setattr(audit_module, "ensure_config", lambda: None)
    return CliRunner().invoke(cli, ["audit", *args])


def test_table_reads_oldest_first_and_names_the_actor(monkeypatch):
    result = _run(monkeypatch)

    assert result.exit_code == 0, result.output
    out = result.output
    # Rich wraps cells to the runner's 80 columns, so order is checked on the whole output
    assert out.index("rent requested") < out.index("created") < out.index("→ DELETING")
    assert out.index("session") < out.index("platform") < out.index("key ci (3f2a9c11)")
    assert "user_initiated" in out


def test_when_column_is_utc_whatever_offset_the_stamp_carries(monkeypatch):
    aware = [_event("pod-create.success", created_at="2026-09-06T04:02:00+02:00"),
             _event("pod-rent.requested", created_at="2026-09-06T04:00:00Z", actor={"auth": "session"})]

    result = _run(monkeypatch, events=aware)

    assert result.exit_code == 0, result.output
    assert "2026-09-06 02:02:00Z" in result.output      # +02:00 wall clock converted, not relabelled
    assert "2026-09-06 04:00:00Z" in result.output
    assert "04:02:00Z" not in result.output


def test_json_prints_the_events_as_returned(monkeypatch):
    result = _run(monkeypatch, "--json")

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == EVENTS_NEWEST_FIRST


def test_since_duration_is_counted_back_from_now(monkeypatch):
    before = datetime.now(timezone.utc) - timedelta(hours=24)

    result = _run(monkeypatch, "--since", "24h", "--json")

    assert result.exit_code == 0, result.output
    [call] = _FakeLium.calls
    assert before <= call["since"] <= datetime.now(timezone.utc) - timedelta(hours=24) + timedelta(seconds=5)


def test_since_accepts_an_iso_timestamp_and_rejects_garbage(monkeypatch):
    ok = _run(monkeypatch, "--since", "2026-09-06T00:00:00Z", "--json")
    [ok_call] = _FakeLium.calls
    bad = _run(monkeypatch, "--since", "yesterday", "--json")

    assert ok.exit_code == 0 and ok_call["since"] == datetime(2026, 9, 6, tzinfo=timezone.utc)
    assert bad.exit_code == EXIT_CONFIGURATION_ERROR and _FakeLium.calls == []


def test_pod_option_resolves_a_listed_pod_by_name(monkeypatch):
    pod = SimpleNamespace(id=POD_ID, huid="brave-fox-3a", name="my-pod")

    result = _run(monkeypatch, "--pod", "my-pod", "--key", KEY_ID, "--limit", "50", "--json", pods=[pod])

    assert result.exit_code == 0, result.output
    assert _FakeLium.calls == [{"since": None, "pod_id": POD_ID, "api_key_id": KEY_ID, "limit": 50}]


def test_pod_option_passes_a_full_id_through_for_a_deleted_pod(monkeypatch):
    result = _run(monkeypatch, "--pod", POD_ID, "--json", pods=[])

    assert result.exit_code == 0, result.output
    assert _FakeLium.calls[0]["pod_id"] == POD_ID


def test_pod_option_rejects_an_unknown_name(monkeypatch):
    result = _run(monkeypatch, "--pod", "nope", "--json", pods=[])

    assert result.exit_code == EXIT_POD_NOT_FOUND
    assert _FakeLium.calls == []


def test_key_option_takes_the_full_id_not_the_eight_characters_the_table_prints(monkeypatch):
    # the server declares api_key_id as a UUID; a prefix would come back as a 422 (exit 3) — refuse it locally
    result = _run(monkeypatch, "--key", KEY_ID[:8], "--json")

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "full id" in result.output and "actor.api_key_id" in result.output
    assert _FakeLium.calls == []


def test_auth_failure_points_at_an_old_backend(monkeypatch):
    result = _run(monkeypatch, error=LiumAuthError("Invalid API key"))

    # a 401 is exit 3 on every command (the CLI's exit-code contract); audit only adds the hint
    assert result.exit_code == EXIT_API_ERROR
    assert "this backend does not yet open" in result.output


def test_auth_failure_keeps_the_servers_hint_and_request_id(monkeypatch):
    # the server's hint and request_id (DAH-3057) are printed under audit's own line, like on any API error
    error = LiumAuthError("Invalid API key", code="invalid_api_key",
                          hint="Create a key at https://lium.io/settings.", request_id="req-401-0001")
    result = _run(monkeypatch, error=error)

    assert result.exit_code == EXIT_API_ERROR
    assert "this backend does not yet open" in result.output
    assert "Create a key at https://lium.io/settings." in result.output
    assert "request_id: req-401-0001" in result.output

    result = _run(monkeypatch, "--json", error=error)
    envelope = json.loads(result.stderr)
    assert envelope["error"]["code"] == "invalid_api_key"
    assert envelope["error"]["hint"] == "Create a key at https://lium.io/settings."
    assert envelope["data"] == {"request_id": "req-401-0001"}


@pytest.mark.parametrize("value", ["0", "1001", "-5", "many"])
def test_limit_outside_1_to_1000_is_refused_before_any_request(monkeypatch, value):
    result = _run(monkeypatch, "--limit", value)

    assert result.exit_code == EXIT_CONFIGURATION_ERROR
    assert "--limit" in result.output
    assert _FakeLium.calls == []


@pytest.mark.parametrize("value", ["1", "1000"])
def test_limit_bounds_are_accepted(monkeypatch, value):
    result = _run(monkeypatch, "--limit", value, "--json")

    assert result.exit_code == 0, result.output
    assert _FakeLium.calls == [{"since": None, "pod_id": None, "api_key_id": None, "limit": int(value)}]


def test_empty_log_says_so(monkeypatch):
    result = _run(monkeypatch, "--since", "1h", events=[])

    assert result.exit_code == 0
    assert "No events since 1h" in result.output


def test_sdk_events_sends_the_filters_as_query_parameters(monkeypatch):
    seen = {}

    def fake_request(self, method, endpoint, **kwargs):
        seen.update(method=method, endpoint=endpoint, params=kwargs.get("params"))
        return SimpleNamespace(json=lambda: [{"id": "e"}])

    monkeypatch.setattr(Lium, "_request", fake_request)
    monkeypatch.setattr(Lium, "__init__", lambda self, *a, **k: None)

    events = Lium().events(since=datetime(2026, 9, 6, tzinfo=timezone.utc), pod_id=POD_ID, api_key_id=KEY_ID, limit=10)

    assert events == [{"id": "e"}]
    assert seen == {
        "method": "GET",
        "endpoint": "/users/me/events",
        "params": {"limit": 10, "since": "2026-09-06T00:00:00+00:00", "pod_id": POD_ID, "api_key_id": KEY_ID},
    }
