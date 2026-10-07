"""`lium volumes`: new names the volume, an empty list says so and clears the index,
rm names what it removes and refuses an old index, and `id:<HUID>` falls back to the live list."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from click.testing import CliRunner

from lium.cli import utils
from lium.cli.volumes.list.command import volumes_list_command
from lium.cli.volumes.new.command import volumes_new_command
from lium.cli.volumes.rm.command import volumes_rm_command


def _volume(huid: str, vid: str = "uuid-1", name: str = "data") -> SimpleNamespace:
    return SimpleNamespace(id=vid, huid=huid, name=name, description="", current_size_gb=0.0)


class _FakeLium:
    def __init__(self, volumes=(), fail_delete: str | None = None):
        self._volumes = list(volumes)
        self.deleted: list[str] = []
        self.fail_delete = fail_delete

    def volumes(self):
        return self._volumes

    def volume_create(self, name, description=""):
        return _volume("brave-fox-1a", name=name)

    def volume_delete(self, volume_id):
        if volume_id == self.fail_delete:
            raise RuntimeError("volume is attached to a pod")
        self.deleted.append(volume_id)


def _patch(monkeypatch, module: str, fake: _FakeLium, tmp_path) -> None:
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    monkeypatch.setattr(f"lium.cli.volumes.{module}.command.ensure_config", lambda: None)
    monkeypatch.setattr(f"lium.cli.volumes.{module}.command.Lium", lambda: fake)


def _cache(tmp_path, volumes, age: timedelta = timedelta()) -> None:
    stamp = (datetime.now(timezone.utc) - age).isoformat()
    rows = [{"id": v.id, "huid": v.huid, "name": v.name} for v in volumes]
    (tmp_path / "last_volumes.json").write_text(json.dumps({"timestamp": stamp, "volumes": rows}))


def test_volumes_new_prints_the_huid_to_attach(monkeypatch, tmp_path) -> None:
    _patch(monkeypatch, "new", _FakeLium(), tmp_path)
    result = CliRunner().invoke(volumes_new_command, ["data"])
    assert result.exit_code == 0, result.output
    assert "id:brave-fox-1a" in result.output


def test_volumes_list_empty_says_so_and_clears_the_index(monkeypatch, tmp_path) -> None:
    _patch(monkeypatch, "list", _FakeLium(), tmp_path)
    _cache(tmp_path, [_volume("old-vol-1")])
    result = CliRunner().invoke(volumes_list_command, [])
    assert result.exit_code == 0, result.output
    assert "No volumes" in result.output
    assert utils.get_last_volume_selection()["volumes"] == []


def test_volumes_rm_prompt_names_the_volume(monkeypatch, tmp_path) -> None:
    asked: list[str] = []
    _patch(monkeypatch, "rm", _FakeLium(), tmp_path)
    monkeypatch.setattr("lium.cli.volumes.rm.command.ui.confirm", lambda msg: asked.append(msg) or False)
    _cache(tmp_path, [_volume("calm-owl-2b")])
    CliRunner().invoke(volumes_rm_command, ["1"])
    assert asked == ["Remove 1 volume: calm-owl-2b (data)?"]


def test_volumes_rm_reports_the_failure_reason(monkeypatch, tmp_path) -> None:
    _patch(monkeypatch, "rm", _FakeLium(fail_delete="uuid-1"), tmp_path)
    _cache(tmp_path, [_volume("calm-owl-2b")])
    result = CliRunner().invoke(volumes_rm_command, ["1", "-y"])
    assert result.exit_code != 0
    assert "volume is attached to a pod" in result.output


def test_volumes_rm_refuses_an_index_older_than_ten_minutes(monkeypatch, tmp_path) -> None:
    fake = _FakeLium()
    _patch(monkeypatch, "rm", fake, tmp_path)
    _cache(tmp_path, [_volume("calm-owl-2b")], age=timedelta(minutes=11))
    result = CliRunner().invoke(volumes_rm_command, ["1", "-y"])
    assert result.exit_code != 0
    assert fake.deleted == []


def test_resolve_volume_huid_falls_back_to_the_live_list(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(utils.config, "config_dir", tmp_path)
    monkeypatch.setattr(utils, "Lium", lambda: _FakeLium([_volume("web-made-3c", vid="uuid-9")]))
    assert utils.resolve_volume_huid("web-made-3c") == "uuid-9"
