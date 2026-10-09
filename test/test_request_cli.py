"""`lium request` / `Lium.request_machine`: a sold-out GPU type becomes a machine request."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.sdk import Lium

CATALOG = ["NVIDIA B300 SXM6 AC", "NVIDIA H100 80GB HBM3", "NVIDIA H100 NVL"]


class _Answer:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _lium(monkeypatch, sent):
    monkeypatch.setattr(Lium, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(Lium, "gpu_types", lambda self: set(CATALOG))

    def request(self, method, endpoint, **kwargs):
        sent.append((method, endpoint, kwargs.get("json")))
        return _Answer({"success": True, "provider_notified_count": 4})

    monkeypatch.setattr(Lium, "_request", request)


def test_request_machine_resolves_a_short_type_to_its_catalog_name(monkeypatch):
    sent = []
    _lium(monkeypatch, sent)

    answer = Lium().request_machine("B300", 8)

    assert sent == [("POST", "/machine-requests", {"machine_name": "NVIDIA B300 SXM6 AC", "gpu_count": 8})]
    assert answer["provider_notified_count"] == 4


def test_request_machine_refuses_a_short_type_covering_several_names(monkeypatch):
    sent = []
    _lium(monkeypatch, sent)

    with pytest.raises(ValueError, match="NVIDIA H100 NVL"):
        Lium().request_machine("H100")
    assert sent == []


def test_request_command_prints_the_answer_as_json(monkeypatch):
    _lium(monkeypatch, [])

    result = CliRunner().invoke(cli, ["request", "NVIDIA H100 NVL", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["provider_notified_count"] == 4
