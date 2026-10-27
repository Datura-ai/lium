"""bk set updates an existing backup schedule in place; bk show names the effective path.

Kept in its own file so this coverage does not depend on test_backup_cli.py
or test_sdk_client.py, which lium#307 removes.
"""

from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from lium.cli.bk.set import command as set_command
from lium.cli.cli import cli
from lium.sdk import Config, Lium


def _pod():
    return SimpleNamespace(
        id="pod-123",
        name="backup-test",
        huid="eager-shark-01",
        status="RUNNING",
        ssh_cmd=None,
        ports={},
        created_at="2026-05-14T00:00:00Z",
        updated_at="2026-05-14T00:00:00Z",
        executor=None,
        template={},
        removal_scheduled_at=None,
        jupyter_installation_status=None,
        jupyter_url=None,
    )


def _patch_backup_command(monkeypatch, command_module, fake_lium_cls):
    monkeypatch.setattr(command_module, "ensure_config", lambda: None)
    monkeypatch.setattr(command_module, "Lium", fake_lium_cls)


def test_bk_set_updates_an_existing_config_in_place(monkeypatch):
    calls = []

    class FakeLium:
        def __init__(self, source="sdk"):
            pass

        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            return SimpleNamespace(id="config-123")

        def backup_update(self, config_id, *, path, frequency_hours, retention_days):
            calls.append(("update", config_id, path, frequency_hours, retention_days))
            return SimpleNamespace(id=config_id)

        def backup_delete(self, config_id):
            calls.append(("delete", config_id))

        def backup_create(self, **kwargs):
            calls.append(("create",))

    _patch_backup_command(monkeypatch, set_command, FakeLium)

    result = CliRunner().invoke(
        cli, ["bk", "set", "backup-test", "--path", "/root/data", "--every", "1d", "--keep", "30d"]
    )

    assert result.exit_code == 0, result.output
    assert calls == [("update", "config-123", "/root/data", 24, 30)]


def test_bk_set_refused_update_keeps_the_old_config(monkeypatch):
    calls = []

    class FakeLium:
        def __init__(self, source="sdk"):
            pass

        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            return SimpleNamespace(id="config-123")

        def backup_update(self, config_id, **kwargs):
            raise RuntimeError("API error 400: Configuration would result in 8760 backups")

        def backup_delete(self, config_id):
            calls.append(("delete", config_id))

    _patch_backup_command(monkeypatch, set_command, FakeLium)

    result = CliRunner().invoke(
        cli, ["bk", "set", "backup-test", "--path", "/root/data", "--every", "1h", "--keep", "365d"]
    )

    assert result.exit_code != 0
    assert calls == []


@pytest.mark.parametrize(
    "flags",
    [
        ["--every", "0h"],
        ["--every", "8d"],
        ["--every", "169h"],
        ["--every", "1d2h"],
        ["--keep", "0d"],
        ["--keep", "400d"],
        ["--keep", "7dx"],
    ],
)
def test_bk_set_refuses_out_of_range_schedule_before_any_request(monkeypatch, flags):
    class FakeLium:
        def __init__(self, source="sdk"):
            raise AssertionError("no request may go out for a refused schedule")

    _patch_backup_command(monkeypatch, set_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "set", "backup-test", "--path", "/root/data", *flags])

    assert result.exit_code == 2, result.output


def test_bk_set_refuses_a_path_with_dot_dot(monkeypatch):
    class FakeLium:
        def __init__(self, source="sdk"):
            raise AssertionError("no request may go out for a refused path")

    _patch_backup_command(monkeypatch, set_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "set", "backup-test", "--path", "/root/../etc"])

    assert result.exit_code == 2
    assert ".." in result.output


def test_backup_update_puts_the_new_schedule_on_the_existing_config(monkeypatch):
    client = Lium(Config(api_key="test"))
    captured = {}

    class Response:
        def json(self):
            return {"id": "config-1", "huid": "config-huid", "pod_executor_id": "pod-1",
                    "backup_frequency_hours": 24, "retention_days": 30, "backup_path": "/root/data"}

    def fake_request(method, endpoint, json=None, **kwargs):
        captured.update(method=method, endpoint=endpoint, payload=json)
        return Response()

    monkeypatch.setattr(client, "_request", fake_request)

    config = client.backup_update("config-1", path="/root/data", frequency_hours=24, retention_days=30)

    assert captured == {
        "method": "PUT",
        "endpoint": "/backup-configs/config-1",
        "payload": {"backup_frequency_hours": 24, "retention_days": 30, "backup_path": "/root/data"},
    }
    assert config.backup_frequency_hours == 24


def test_bk_show_names_the_default_path_when_the_api_returns_none(monkeypatch):
    from lium.cli.bk.show import command as show_command

    class FakeLium:
        def __init__(self, source="sdk"):
            pass

        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            return SimpleNamespace(id="config-123", backup_path="", backup_frequency_hours=6,
                                   retention_days=2, next_backup_at=None)

        def backup_logs(self, pod):
            return []

    _patch_backup_command(monkeypatch, show_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "show", "backup-test"])

    assert result.exit_code == 0, result.output
    assert "path=/root," in result.output
