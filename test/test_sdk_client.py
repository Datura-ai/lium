import warnings
from types import SimpleNamespace

import pytest

from lium.sdk import Config, Lium, LiumError, LiumPermissionError


class _Forbidden:
    """A 403 response, usable both directly and as a streaming context manager."""

    ok = False
    status_code = 403
    text = "User is not verified"

    def json(self):
        raise ValueError("not a JSON body")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_client_sets_version_header(monkeypatch):
    monkeypatch.setattr("lium.sdk.client._get_client_version", lambda: "1.2.3")

    client = Lium(Config(api_key="test"), source="cli")

    assert client.headers["X-API-KEY"] == "test"
    assert client.headers["X-Source"] == "cli"
    assert client.headers["X-Lium-Client-Version"] == "1.2.3"


def test_request_403_raises_permission_error(monkeypatch):
    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *a, **kw: _Forbidden()
    )
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumPermissionError):
        client._request("GET", "/pods")


def test_logs_403_raises_permission_error(monkeypatch):
    monkeypatch.setattr("lium.sdk.client.requests.request", lambda *a, **kw: _Forbidden())
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumPermissionError):
        list(client.logs("pod-1"))


def test_restore_uses_pod_safe_default_path(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured = {}
    pod = SimpleNamespace(id="pod-1", default_restore_path="/workspace/restored")

    class Response:
        def json(self):
            return {"success": True}

    def fake_request(method, endpoint, json=None, **kwargs):
        captured.update(method=method, endpoint=endpoint, payload=json)
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    client.restore(pod, backup_id="backup-123")

    assert captured["endpoint"] == "/pods/pod-1/restore"
    assert captured["payload"]["restore_path"] == "/workspace/restored"


def test_backup_create_requires_explicit_path():
    client = Lium(Config(api_key="test"))
    pod = SimpleNamespace(id="pod-1", volume_path="/root")

    with pytest.raises(TypeError, match="path"):
        client.backup_create(pod)


def test_backup_create_warns_for_explicit_whole_volume(monkeypatch):
    client = Lium(Config(api_key="test"))
    pod = SimpleNamespace(id="pod-1", volume_path="/root")
    captured = {}

    class Response:
        def json(self):
            return {
                "id": "config-1",
                "huid": "config-huid",
                "pod_executor_id": "pod-1",
                "backup_frequency_hours": 6,
                "retention_days": 7,
                "backup_path": "/root",
            }

    def fake_request(method, endpoint, json=None, **kwargs):
        captured.update(method=method, endpoint=endpoint, payload=json)
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    with pytest.warns(UserWarning, match="entire volume"):
        client.backup_create(pod, path="/root")

    assert captured == {
        "method": "POST",
        "endpoint": "/backup-configs",
        "payload": {
            "pod_id": "pod-1",
            "backup_frequency_hours": 6,
            "retention_days": 7,
            "backup_path": "/root",
        },
    }


def test_backup_create_skips_sdk_warning_for_cli_callers(monkeypatch):
    client = Lium(Config(api_key="test"), source="cli")
    pod = SimpleNamespace(id="pod-1", volume_path="/root")

    class Response:
        def json(self):
            return {
                "id": "config-1",
                "huid": "config-huid",
                "pod_executor_id": "pod-1",
                "backup_frequency_hours": 6,
                "retention_days": 7,
                "backup_path": "/root",
            }

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response())

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        client.backup_create(pod, path="/root")

    assert caught_warnings == []


def test_restore_log_hydrates_progress_metadata():
    client = Lium(Config(api_key="test"))

    restore_log = client._dict_to_restore_log(
        {
            "id": "restore-1",
            "backup_id": "backup-1",
            "pod_id": "pod-1",
            "status": "IN_PROGRESS",
            "progress": 12.34,
            "created_at": "2026-08-12T12:00:00Z",
            "backup_engine": "RESTIC",
            "restore_mode": "STARTUP",
            "stage": "RESTORING",
            "last_heartbeat_at": "2026-08-12T12:01:00Z",
            "total_files": 100,
            "processed_files": 25,
            "total_bytes": 1_000,
            "processed_bytes": 250,
            "elapsed_seconds": 12,
            "throughput_bytes_per_second": 20,
            "estimated_remaining_seconds": 38,
        }
    )

    assert restore_log.stage == "RESTORING"
    assert restore_log.restore_mode == "STARTUP"
    assert restore_log.processed_files == 25
    assert restore_log.processed_bytes == 250
    assert restore_log.elapsed_seconds == 12
    assert restore_log.estimated_remaining_seconds == 38


def test_backup_and_restore_lifecycle_methods_use_distinct_endpoints(monkeypatch):
    client = Lium(Config(api_key="test"))
    calls = []

    class Response:
        def json(self):
            return {"success": True}

    def fake_request(method, endpoint, **kwargs):
        calls.append((method, endpoint))
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    client.backup_cancel("backup-1")
    client.backup_log_delete("backup-1")
    client.restore_cancel("restore-1")

    assert calls == [
        ("POST", "/backup-logs/backup-1/cancel"),
        ("DELETE", "/backup-logs/backup-1"),
        ("POST", "/restore-logs/restore-1/cancel"),
    ]


def test_structured_busy_error_is_readable(monkeypatch):
    class ConflictResponse:
        ok = False
        status_code = 409
        text = ""

        def json(self):
            return {
                "detail": {
                    "code": "BACKUP_STORAGE_BUSY",
                    "message": "Another backup or restore is already running",
                    "active_operation_id": "active-123",
                }
            }

    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *args, **kwargs: ConflictResponse()
    )
    client = Lium(Config(api_key="test"))

    with pytest.raises(
        LiumError, match="Another backup or restore is already running.*active-123"
    ):
        client._request("POST", "/pods/pod-1/backup")


def test_structured_message_error_is_readable(monkeypatch):
    class ConflictResponse:
        ok = False
        status_code = 409
        text = ""

        def json(self):
            return {
                "message": {
                    "code": "BACKUP_NOT_ACTIVE",
                    "message": "This backup is no longer active",
                    "current_status": "COMPLETED",
                }
            }

    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *args, **kwargs: ConflictResponse()
    )
    client = Lium(Config(api_key="test"))

    with pytest.raises(LiumError, match="This backup is no longer active"):
        client._request("POST", "/backup-logs/8fbb30f6/cancel")


def test_validation_error_includes_field_and_reason(monkeypatch):
    class ValidationResponse:
        ok = False
        status_code = 422
        text = ""

        def json(self):
            return {
                "message": "Request validation failed",
                "validation_errors": [
                    {
                        "field": "body -> backup_log_id",
                        "message": "Input should be a valid UUID",
                        "type": "uuid_parsing",
                    }
                ],
            }

    monkeypatch.setattr(
        "lium.sdk.client.requests.request", lambda *args, **kwargs: ValidationResponse()
    )
    client = Lium(Config(api_key="test"))

    with pytest.raises(
        LiumError, match="body -> backup_log_id: Input should be a valid UUID"
    ):
        client._request("POST", "/executors/executor-1/rent")


def test_resolve_backup_id_uses_paginated_backup_logs(monkeypatch):
    client = Lium(Config(api_key="test"))
    backup_id = "8fbb30f6-6026-4043-98c7-c4189dc09bef"

    class Response:
        def json(self):
            return {"items": [{"id": backup_id}], "has_next": False}

    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: Response())

    assert client.resolve_backup_id("8fbb30f6") == backup_id
    assert client.resolve_backup_id("8FBB30F6") == backup_id


def test_backup_log_uses_authenticated_single_log_endpoint(monkeypatch):
    client = Lium(Config(api_key="test"))
    backup_id = "8fbb30f6-6026-4043-98c7-c4189dc09bef"
    request = SimpleNamespace()

    class Response:
        def json(self):
            return {"id": backup_id, "status": "COMPLETED"}

    def fake_request(method, endpoint, **kwargs):
        request.method = method
        request.endpoint = endpoint
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    backup_log = client.backup_log(backup_id)

    assert request.method == "GET"
    assert request.endpoint == f"/backup-logs/{backup_id}"
    assert backup_log.id == backup_id


def test_resolve_restore_id_searches_active_pods(monkeypatch):
    client = Lium(Config(api_key="test"))
    restore_id = "9b6c8d90-1111-4222-9333-48b031f1f3eb"
    pod = SimpleNamespace(id="pod-1")
    monkeypatch.setattr(client, "ps", lambda: [pod])
    monkeypatch.setattr(
        client,
        "restore_logs",
        lambda candidate: [SimpleNamespace(id=restore_id)] if candidate is pod else [],
    )

    assert client.resolve_restore_id("9b6c8d90") == restore_id
    assert client.resolve_restore_id("9B6C8D90") == restore_id


def test_ps_prices_a_split_rental_per_pod_gpu_and_leaves_the_host_count_alone(monkeypatch):
    """1 GPU rented of a 3×RTX 3090 node: PodInfo.gpu_count is 1, the executor keeps the host's 3, $/GPU is $/h over 1."""
    payload = [{
        "id": "d7b3e3b2-0f7c-4f7e-9c3c-0b3f1a2e9a01", "pod_name": "sx-ctl", "status": "RUNNING",
        "gpu_count": "1", "gpu_name": "NVIDIA GeForce RTX 3090", "price": 0.18,
        "ssh_connect_cmd": "ssh root@pod.invalid -p 2222", "ports_mapping": {"22": 2222},
        "created_at": "2026-09-07T01:51:40", "updated_at": "2026-09-07T01:52:00", "template": {"name": "Pytorch"},
        "executor": {
            "id": "e0a7c1e2-6c2e-4d3d-9d8b-0f1a2b3c4d5e", "machine_name": "NVIDIA GeForce RTX 3090",
            "gpu_count": 3, "price_per_gpu": None, "executor_ip_address": "pod.invalid",
            "specs": {"gpu": {"count": 3, "details": [{"name": "NVIDIA GeForce RTX 3090", "capacity": 24576}] * 3}},
            "location": {"country": "Germany", "country_code": "DE"},
        },
    }]
    monkeypatch.setattr(Lium, "_request", lambda self, *a, **kw: SimpleNamespace(json=lambda: payload))

    pod = Lium(Config(api_key="test")).ps()[0]

    assert pod.gpu_count == 1
    assert pod.executor.gpu_count == 3
    assert pod.executor.price_per_hour == 0.18
    assert pod.executor.price_per_gpu == 0.18


def test_ps_prices_per_host_gpu_when_the_pod_count_is_malformed(monkeypatch):
    """A non-numeric pod.gpu_count must not break `lium ps`: PodInfo.gpu_count is None and $/GPU falls back to the host's count."""
    payload = [{
        "id": "d7b3e3b2-0f7c-4f7e-9c3c-0b3f1a2e9a02", "pod_name": "sx-bad", "status": "RUNNING",
        "gpu_count": "three", "price": 0.54,
        "executor": {
            "id": "e0a7c1e2-6c2e-4d3d-9d8b-0f1a2b3c4d5f", "machine_name": "NVIDIA GeForce RTX 3090",
            "gpu_count": 3, "price_per_gpu": None, "executor_ip_address": "pod.invalid",
            "specs": {"gpu": {"count": 3, "details": [{"name": "NVIDIA GeForce RTX 3090", "capacity": 24576}] * 3}},
            "location": {"country": "Germany", "country_code": "DE"},
        },
    }]
    monkeypatch.setattr(Lium, "_request", lambda self, *a, **kw: SimpleNamespace(json=lambda: payload))

    pod = Lium(Config(api_key="test")).ps()[0]

    assert pod.gpu_count is None
    assert pod.executor.gpu_count == 3
    assert pod.executor.price_per_gpu == pytest.approx(0.18)


class _FakeChannel:
    """Two stdout chunks, one stderr chunk, then the exit status."""

    def __init__(self):
        self.out = [b"one\n", b"two\n"]
        self.err = [b"warn\n"]
        self.polls = 0

    def recv_ready(self):
        return bool(self.out)

    def recv(self, n):
        return self.out.pop(0)

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv_stderr(self, n):
        return self.err.pop(0)

    def exit_status_ready(self):
        self.polls += 1
        return self.polls > 1  # first poll: not finished yet

    def recv_exit_status(self):
        return 3


def _stream_client(monkeypatch, channel):
    from contextlib import contextmanager

    calls = {}

    class _Stdin:
        def write(self, data):
            calls["stdin"] = data

        def close(self):
            pass

    class _Std:
        def __init__(self, ch):
            self.channel = ch

    class _SSH:
        def exec_command(self, command, get_pty=False):
            calls["command"] = command
            calls["get_pty"] = get_pty
            return _Stdin(), _Std(channel), _Std(channel)

    client = Lium(Config(api_key="test"))

    @contextmanager
    def fake_connection(pod, timeout=30):
        yield _SSH()

    monkeypatch.setattr(client, "ssh_connection", fake_connection)
    monkeypatch.setattr("lium.sdk.client.time.sleep", lambda s: None)
    return client, calls


def test_stream_exec_without_pty_keeps_streams_apart_and_returns_exit_status(monkeypatch):
    client, calls = _stream_client(monkeypatch, _FakeChannel())
    pod = SimpleNamespace(id="pod-1", name="p", ssh_cmd="ssh root@10.0.0.1 -p 22")

    gen = client.stream_exec(pod, command="python -u run.py", env={"A": "x y"}, pty=False)
    chunks = []
    while True:
        try:
            chunks.append(next(gen))
        except StopIteration as stop:
            exit_code = stop.value
            break

    assert chunks == [  # one stdout and one stderr read per loop turn
        {"type": "stdout", "data": "one\n"},
        {"type": "stderr", "data": "warn\n"},
        {"type": "stdout", "data": "two\n"},
    ]
    assert exit_code == 3
    assert calls["get_pty"] is False
    assert calls["command"] == "export A='x y' && python -u run.py"  # main's shlex-quoted exports (DAH-2894)


def test_stream_exec_default_pty_is_unchanged(monkeypatch):
    client, calls = _stream_client(monkeypatch, _FakeChannel())
    pod = SimpleNamespace(id="pod-1", name="p", ssh_cmd="ssh root@10.0.0.1 -p 22")

    list(client.stream_exec(pod, command="ls", env={"A": "1"}))

    assert calls["get_pty"] is True
    assert calls["command"] == "export A=1 && ls"
