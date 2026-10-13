"""`lium record` and `Lium.pod_record`: a run's record, readable after removal."""

import json

import pytest
from click.testing import CliRunner

from lium.cli.cli import cli
from lium.cli.record import command as record_module
from lium.sdk import Config, Lium, LiumError, LiumNotFoundError

POD = "3f2b9c1e-8a4d-4b6e-9f10-2c3d4e5f6a7b"
RECORD = {
    "pod_id": POD, "pod_name": "train-1", "removed": True, "gpu_name": "NVIDIA H100 80GB HBM3", "gpu_count": 8,
    "executor_id": "9d0c2f6e-1111-4222-8333-444455556666", "image": "pytorch/pytorch@sha256:abc",
    "created_at": "2026-10-10T08:00:00Z", "removed_at": "2026-10-10T10:00:00Z", "close_reason": "user_initiated",
    "environment": {"recorded_at": "2026-10-10T08:01:00Z", "template_id": None, "image": "pytorch/pytorch@sha256:abc",
                    "gpu_driver": "570.133.20", "cuda_driver": "12.8", "gpu_interconnect": "nvlink"},
    "billed_seconds": 7200.0, "total": 32.0, "price_segments": [],
}


class _Response:
    def json(self):
        return RECORD


@pytest.fixture
def client(monkeypatch):
    lium = Lium(Config(api_key="test"))
    lium.seen = []
    monkeypatch.setattr(lium, "_request", lambda method, path, **kw: lium.seen.append((method, path)) or _Response())
    monkeypatch.setattr(record_module, "Lium", lambda: lium)
    return lium


def test_record_by_uuid_reads_the_route_once_without_listing_pods(client, monkeypatch):
    monkeypatch.setattr(client, "ps", lambda: pytest.fail("a UUID needs no pod list: a removed pod is not in it"))

    out = CliRunner().invoke(cli, ["record", POD, "--format", "json"])

    assert out.exit_code == 0, out.output
    assert client.seen == [("GET", f"/pods/{POD}/record")]
    assert json.loads(out.output)["environment"]["gpu_interconnect"] == "nvlink"


def test_record_table_shows_the_run(client):
    out = CliRunner().invoke(cli, ["record", POD])

    assert out.exit_code == 0, out.output
    for text in ("8× NVIDIA H100 80GB HBM3", "pytorch/pytorch@sha256:abc", "570.133.20 / CUDA 12.8", "nvlink",
                 "user_initiated", "$32.0000 for 2.00 h billed", "(removed)"):
        assert text in out.output, text


def test_record_of_a_name_that_matches_no_running_pod_fails_without_a_request(client, monkeypatch):
    monkeypatch.setattr(client, "ps", lambda: [])

    out = CliRunner().invoke(cli, ["record", "no-such-pod"])

    assert out.exit_code != 0 and client.seen == []


# The fields of lium-platform#1204's PodRunRecord / PodRunEnvironment (dtos/billing_history.py), which
# GET /pods/{id}/record returns; RECORD above must be that response so the table is tested against it.
SERVER_RECORD_FIELDS = {
    "pod_id", "pod_name", "removed", "gpu_name", "gpu_count", "executor_id", "image", "created_at", "removed_at",
    "close_reason", "environment", "billed_seconds", "total", "price_segments",
}
SERVER_ENVIRONMENT_FIELDS = {"recorded_at", "template_id", "image", "gpu_driver", "cuda_driver", "gpu_interconnect"}


def test_the_fixture_is_the_servers_record_shape():
    assert set(RECORD) == SERVER_RECORD_FIELDS
    assert set(RECORD["environment"]) == SERVER_ENVIRONMENT_FIELDS


def _missing_route(lium, monkeypatch, features):
    class _Version:
        def json(self):
            return {"features": features}

    def request(method, path, **kw):
        lium.seen.append((method, path))
        if path == "/version":
            return _Version()
        raise LiumNotFoundError("Resource not found: Not Found")

    monkeypatch.setattr(lium, "_request", request)


def test_a_404_from_a_server_without_run_records_says_so_instead_of_pod_not_found(client, monkeypatch):
    _missing_route(client, monkeypatch, ["rent_by_spec"])

    with pytest.raises(LiumError) as raised:
        client.pod_record(POD)

    assert not isinstance(raised.value, LiumNotFoundError)
    assert "does not serve run records" in str(raised.value)
    assert client.seen == [("GET", f"/pods/{POD}/record"), ("GET", "/version")]


def test_a_404_from_a_server_with_run_records_is_a_pod_not_found(client, monkeypatch):
    _missing_route(client, monkeypatch, ["rent_by_spec", "run_record"])

    with pytest.raises(LiumNotFoundError):
        client.pod_record(POD)
