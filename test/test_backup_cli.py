from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli.bk.now import command as now_command
from lium.cli.bk.logs import command as logs_command
from lium.cli.bk.restore import command as restore_command
from lium.cli.bk.rm import command as rm_command
from lium.cli.bk.set import command as set_command
from lium.cli.bk.show import command as show_command
from lium.cli.cli import cli


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


def test_bk_set_prints_success(monkeypatch):
    class FakeLium:
        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            assert pod.id == "pod-123"
            return None

        def backup_create(self, *, pod, path, frequency_hours, retention_days):
            assert pod.id == "pod-123"
            assert path == "/root/data"
            assert frequency_hours == 6
            assert retention_days == 7
            return SimpleNamespace(id="config-123")

    _patch_backup_command(monkeypatch, set_command, FakeLium)

    result = CliRunner().invoke(
        cli,
        ["bk", "set", "backup-test", "--path", "/root/data", "--every", "6h", "--keep", "7d"],
    )

    assert result.exit_code == 0
    assert "Backup configured for backup-test" in result.output
    assert "path=/root/data, every=6h, keep=7d" in result.output


def test_bk_now_prints_backup_log_id(monkeypatch):
    class FakeLium:
        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            assert pod.id == "pod-123"
            return SimpleNamespace(id="config-123")

        def backup_now(self, *, pod, name, description):
            assert pod.id == "pod-123"
            assert name == "pre-deploy"
            assert description == "before release"
            return {"backup_log_id": "8fbb30f6-6026-4043-98c7-c4189dc09bef"}

    _patch_backup_command(monkeypatch, now_command, FakeLium)

    result = CliRunner().invoke(
        cli,
        ["bk", "now", "backup-test", "--name", "pre-deploy", "--description", "before release"],
    )

    assert result.exit_code == 0
    assert "Backup started for backup-test (id=8fbb30f6)" in result.output


def test_bk_logs_by_id_prints_status_progress_and_error(monkeypatch):
    backup_log = SimpleNamespace(
        id="8fbb30f6-6026-4043-98c7-c4189dc09bef",
        status="FAILED",
        progress=30,
        created_at="2026-05-14T12:00:00Z",
        completed_at=None,
        error_message="Backup upload failed",
    )

    class FakeLium:
        def ps(self):
            return [_pod()]

        def backup_logs(self, pod):
            assert pod.id == "pod-123"
            return [backup_log]

    _patch_backup_command(monkeypatch, logs_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "logs", "--id", "8fbb30f6"])

    assert result.exit_code == 0
    assert "Pod: backup-test" in result.output
    assert "Status: FAILED" in result.output
    assert "Progress: 30%" in result.output
    assert "Error: Backup upload failed" in result.output


def test_bk_rm_prints_success(monkeypatch):
    class FakeLium:
        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            assert pod.id == "pod-123"
            return SimpleNamespace(id="config-123")

        def backup_delete(self, config_id):
            assert config_id == "config-123"
            return {"success": True}

    _patch_backup_command(monkeypatch, rm_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "rm", "backup-test", "--yes"])

    assert result.exit_code == 0
    assert "Backup configuration removed for backup-test" in result.output


def test_bk_restore_prints_success(monkeypatch):
    class FakeLium:
        def ps(self):
            return [_pod()]

        def restore(self, *, pod, backup_id, restore_path):
            assert pod.id == "pod-123"
            assert backup_id == "backup-123"
            assert restore_path == "/root/restore"
            return {"restore_log_id": "restore-123"}

    _patch_backup_command(monkeypatch, restore_command, FakeLium)

    result = CliRunner().invoke(
        cli,
        ["bk", "restore", "backup-test", "--id", "backup-123", "--to", "/root/restore", "--yes"],
    )

    assert result.exit_code == 0
    assert "Restore started for backup-test at /root/restore" in result.output


def test_bk_show_warns_when_config_missing(monkeypatch):
    class FakeLium:
        def ps(self):
            return [_pod()]

        def backup_config(self, pod):
            assert pod.id == "pod-123"
            return None

    _patch_backup_command(monkeypatch, show_command, FakeLium)

    result = CliRunner().invoke(cli, ["bk", "show", "backup-test"])

    assert result.exit_code == 0
    assert "No backup configuration found for backup-test" in result.output
