"""Unit tests for ``Lium.ls()`` client-side CUDA version filtering."""

from __future__ import annotations

from lium.sdk import Config, Lium


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _make_executor_dict(executor_id: str, max_cuda_version) -> dict:
    """Build a minimal executor dict as returned by the /executors endpoint."""
    return {
        "id": executor_id,
        "machine_name": "NVIDIA H100 SXM 8x",
        "executor_ip_address": "1.2.3.4",
        "price_per_gpu": 2.5,
        "status": "available",
        "location": {"country": "US"},
        "specs": {
            "gpu": {
                "count": 8,
                "details": [{"name": "H100", "capacity": 81920, "pcie_speed": 16}],
                "driver": "535.104.05",
            },
            "ram": {"total": 2097152},
            "hard_disk": {"total": 10485760},
            "sysbox_runtime": False,
        },
        "max_cuda_version": max_cuda_version,
        "effective_upload_speed_mbps": 500.0,
        "effective_download_speed_mbps": 1000.0,
    }


def _fake_executors():
    """Three executors: one above threshold, one below, one with None."""
    return [
        _make_executor_dict("above-threshold", 12.6),
        _make_executor_dict("below-threshold", 11.8),
        _make_executor_dict("cuda-unknown", None),
    ]


def test_ls_cuda_filter_keeps_only_above_threshold(monkeypatch):
    """Only the executor whose max_cuda_version >= min_cuda_version is returned."""
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        return _Response(_fake_executors())

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.ls(min_cuda_version=12.4)

    ids = [e.id for e in result]
    assert ids == ["above-threshold"], f"Expected only above-threshold, got {ids}"


def test_ls_cuda_filter_excludes_none(monkeypatch):
    """Executors with max_cuda_version=None are excluded when min_cuda_version is set."""
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        return _Response(_fake_executors())

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.ls(min_cuda_version=12.4)

    assert all(e.max_cuda_version is not None for e in result)


def test_ls_no_cuda_filter_returns_all(monkeypatch):
    """When min_cuda_version is not set, all executors are returned."""
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        return _Response(_fake_executors())

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.ls()

    assert len(result) == 3


def test_ls_cuda_filter_exact_boundary_included(monkeypatch):
    """An executor whose max_cuda_version equals min_cuda_version is included."""
    client = Lium(Config(api_key="test-key"))

    executors = [_make_executor_dict("exact", 12.4)]

    def fake_request(method, endpoint, **kwargs):
        return _Response(executors)

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.ls(min_cuda_version=12.4)

    assert len(result) == 1
    assert result[0].id == "exact"


def test_ls_cuda_version_stored_on_executor_info(monkeypatch):
    """max_cuda_version is correctly mapped onto ExecutorInfo."""
    client = Lium(Config(api_key="test-key"))

    def fake_request(method, endpoint, **kwargs):
        return _Response([_make_executor_dict("e1", 12.6)])

    monkeypatch.setattr(client, "_request", fake_request)

    result = client.ls()

    assert result[0].max_cuda_version == 12.6
