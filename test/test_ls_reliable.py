"""`lium ls --reliable` / `Lium.ls(reliable=True)`: hides nodes whose rents often fail to start."""

from __future__ import annotations

from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.ls import command as ls_command_module
from lium.sdk import Config, ExecutorInfo, Lium


# helpers from test/test_ls_interconnect.py, inlined when that file was cut
def _executor_dict(executor_id: str, **extra) -> dict:
    """Minimal executor dict as returned by GET /executors, plus whatever the test adds."""
    base = {
        "id": executor_id,
        "machine_name": "NVIDIA H200 8x",
        "executor_ip_address": "203.0.113.4",
        "price_per_gpu": 3.25,
        "status": "available",
        "location": {"country": "United States", "country_code": "US"},
        "specs": {
            "gpu": {"count": 8, "details": [{"name": "H200", "capacity": 143771, "pcie_speed": 32}], "driver": "570.86"},
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "network": {"upload_speed": 480.0, "download_speed": 300.0},
            "sysbox_runtime": False,
        },
        "effective_upload_speed_mbps": 480.0,
        "effective_download_speed_mbps": 300.0,
        "max_cuda_version": 12.8,
        "tier": "secure",
    }
    base.update(extra)
    return base

def _map(executor_dict: dict) -> ExecutorInfo:
    return Lium(Config(api_key="test-key"))._dict_to_executor_info(executor_dict)

class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

def _client_with(payload, captured: dict) -> Lium:
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        captured.update(kwargs.get("params") or {})
        return _Response(payload)

    client._request = fake_request  # type: ignore[method-assign]
    return client

def _run_ls(monkeypatch, fleet: list[ExecutorInfo], *args: str):
    class _FakeLium:
        workspaces = SimpleNamespace(current=lambda: None)   # a server without workspaces: no context line (main since lium#152)

        def __init__(self, *a, **k):
            pass

        def unknown_gpu_type(self, gpu_type):
            # the marketplace knows H100 and H200; anything else is a typo
            return None if gpu_type.upper() in ("H100", "H200") else ["H100", "H200"]

        def ls(self, **kwargs):
            _FakeLium.kwargs = kwargs
            result = fleet
            if kwargs.get("gpu_type"):
                result = [e for e in result if kwargs["gpu_type"].upper() in (e.gpu_type or "").upper()]
            if kwargs.get("nvlink"):
                result = [e for e in result if e.nvlink is True]
            if kwargs.get("min_download_mbps") is not None:
                result = [e for e in result if (e.effective_download_speed_mbps or 0) >= kwargs["min_download_mbps"]]
            return result

    monkeypatch.setattr(ls_command_module, "Lium", _FakeLium)
    monkeypatch.setattr(ls_command_module, "store_executor_selection", lambda executors: None)
    return CliRunner().invoke(cli, ["ls", *args]), _FakeLium



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
