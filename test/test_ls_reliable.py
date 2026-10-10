"""`lium ls --reliable` / `Lium.ls(reliable=True)`: only providers whose reliability score rests on enough rentals."""

from __future__ import annotations

from test_ls_interconnect import _client_with, _executor_dict, _map, _run_ls


def _fleet() -> list[dict]:
    return [
        _executor_dict("proven", reliability_proven=True),
        _executor_dict("thin", reliability_proven=False),
        _executor_dict("old-backend"),  # an API that predates the verdict
    ]


def test_ls_reliable_sends_the_server_parameter_and_filters_client_side():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls(reliable=True)

    assert captured["reliable_only"] == "true"
    assert [e.id for e in result] == ["proven"]


def test_ls_without_reliable_keeps_every_node():
    captured: dict = {}
    client = _client_with(_fleet(), captured)

    result = client.ls()

    assert "reliable_only" not in captured
    assert [e.id for e in result] == ["proven", "thin", "old-backend"]


def test_reliability_proven_is_none_when_the_api_does_not_send_it():
    assert _map(_executor_dict("old-backend")).reliability_proven is None


def test_ls_reliable_flag_reaches_the_sdk(monkeypatch):
    fleet = [_map(d) for d in _fleet()]

    result, fake = _run_ls(monkeypatch, fleet, "--reliable", "--format", "json")

    assert result.exit_code == 0, result.output
    assert fake.kwargs["reliable"] is True
