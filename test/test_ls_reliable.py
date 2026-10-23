"""`lium ls --reliable` / `Lium.ls(reliable=True)`: hides nodes whose rents often fail to start."""

from __future__ import annotations

from test_ls_interconnect import _client_with, _executor_dict, _map, _run_ls


def _fleet() -> list[dict]:
    return [
        _executor_dict("healthy", frequent_rent_failures=False),
        _executor_dict("failing", frequent_rent_failures=True),
        _executor_dict("old-backend"),  # an API that predates the verdict
    ]


def test_ls_reliable_sends_the_server_parameter_and_filters_client_side():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls(reliable=True)

    assert captured["reliable_only"] == "true"
    assert [e.id for e in result] == ["healthy", "old-backend"]


def test_ls_without_reliable_keeps_every_node():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls()

    assert "reliable_only" not in captured
    assert [e.id for e in result] == ["healthy", "failing", "old-backend"]


def test_frequent_rent_failures_is_none_when_the_api_does_not_send_it():
    assert _map(_executor_dict("old-backend")).frequent_rent_failures is None


def test_ls_reliable_flag_reaches_the_sdk(monkeypatch):
    fleet = [_map(d) for d in _fleet()]

    result, fake = _run_ls(monkeypatch, fleet, "--reliable", "--format", "json")

    assert result.exit_code == 0, result.output
    assert fake.kwargs["reliable"] is True


def test_nvlink_and_reliable_no_match_names_both_filters(monkeypatch):
    result, _ = _run_ls(monkeypatch, [], "--nvlink", "--reliable")

    assert result.exit_code == 0, result.output
    assert "NVLink between every GPU pair and rents that rarely fail to start" in " ".join(result.output.split())
    assert "--reliable" in result.output
